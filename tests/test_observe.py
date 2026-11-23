"""Test my remote: listen-only observation and its single outcome."""

import asyncio
from pathlib import Path

import pytest

from custom_components.bt_remote_voice.hid_descriptor import KeyUsage
from custom_components.bt_remote_voice.observe import (
    RESULTS,
    Observation,
    observe,
)

DESCRIPTOR = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "fire_0413_report_descriptor.hex").read_text().strip()
)
VOICE_DOWN = bytes.fromhex("0221020000")
RELEASE = bytes.fromhex("0200000000")
ARROW_UP = bytes.fromhex("01000052")


class Node:
    def __init__(self, reports):
        self.queue = asyncio.Queue()
        for report in reports:
            self.queue.put_nowait(report)
        self.closed = False
        self.writes = []

    async def read(self):
        return await self.queue.get()

    def write(self, data):  # an observation must never call this
        self.writes.append(data)

    def close(self):
        self.closed = True


class Atvv:
    """A fake ATVV transport that delivers control frames after open."""

    def __init__(self, frames=(), error=None):
        self.frames, self.error, self.closed, self.writes = frames, error, False, []

    async def open(self, address, on_control, on_audio):
        if self.error:
            raise self.error
        for frame in self.frames:
            on_control(frame)
        on_audio(b"\x00" * 10)
        return None  # caps are only needed to pick the v0.4 wire format

    async def close(self):
        self.closed = True


async def run(family="atvv", *, atvv=None, node=None, nodes=("/dev/hidraw3",), seconds=0.6):
    opened = []

    def opener(path):
        opened.append(path)
        return node or Node([])

    return await observe(
        "02:00:00:00:00:01",
        family,
        seconds,
        factory=lambda: atvv or Atvv(),
        opener=opener,
        find_all=lambda address: list(nodes),
        read=lambda path: DESCRIPTOR,
    )


async def test_fire_voice_key_is_recognised_and_nothing_is_written():
    node = Node([VOICE_DOWN, RELEASE, VOICE_DOWN, RELEASE])
    obs = await run("fire", node=node)
    assert obs.result == "fire_voice"
    assert obs.keys == {KeyUsage(0x0C, 0x0221): 2}  # presses, not repeated reports
    assert node.writes == [] and node.closed


async def test_repeated_reports_of_a_held_key_count_as_one_press():
    obs = await run("fire", node=Node([VOICE_DOWN, VOICE_DOWN, VOICE_DOWN, RELEASE]))
    assert obs.keys == {KeyUsage(0x0C, 0x0221): 1}


async def test_a_plain_key_only_is_reported_as_plain_key():
    obs = await run("fire", node=Node([ARROW_UP, RELEASE]))
    assert obs.result == "plain_key" and obs.keys == {KeyUsage(0x07, 0x52): 1}


async def test_the_voice_usage_on_an_atvv_remote_is_only_a_plain_key():
    obs = await run("atvv", node=Node([VOICE_DOWN, RELEASE]))
    assert obs.result == "plain_key"


async def test_atvv_hold_start_is_recognised():
    obs = await run("atvv", atvv=Atvv([bytes([4, 3, 2, 1])]))  # start, reason 3 (hold)
    assert obs.result == "atvv_hold"
    assert obs.atvv_events == [("start", 3, 2)] and obs.audio_frames == 1


async def test_atvv_search_is_recognised_as_tap():
    obs = await run("atvv", atvv=Atvv([bytes([8])]))  # search notification
    assert obs.result == "atvv_tap"


async def test_atvv_hold_beats_keys_seen_at_the_same_time():
    obs = await run("atvv", atvv=Atvv([bytes([4, 3, 2, 1])]), node=Node([VOICE_DOWN]))
    assert obs.result == "atvv_hold" and obs.keys


async def test_nothing_seen_reports_nothing_and_the_reason():
    obs = await run("atvv", nodes=(), atvv=Atvv(error=ConnectionError("wake_remote")))
    assert obs.result == "nothing"
    assert set(obs.errors) == {"no_hidraw_node", "wake_remote"}


async def test_a_permission_error_is_a_distinct_reason():
    obs = await observe(
        "02:00:00:00:00:01",
        "fire",
        0.1,
        opener=lambda path: (_ for _ in ()).throw(PermissionError()),
        find_all=lambda a: ["/dev/hidraw3"],
        read=lambda p: DESCRIPTOR,
    )
    assert obs.result == "nothing" and obs.errors == ["hidraw_permission_denied"]


async def test_unforeseen_errors_become_a_result_not_a_crash():
    obs = await run("atvv", atvv=Atvv(error=RuntimeError("boom")))
    assert "bluetooth_unavailable" in obs.errors
    obs = await run("atvv", atvv=Atvv(error=ValueError("weird")))
    assert "bluetooth_operation_failed" in obs.errors


async def test_a_node_that_appears_after_the_test_started_is_picked_up():
    calls = []

    def find_all(address):
        calls.append(1)
        return [] if len(calls) < 2 else ["/dev/hidraw3"]

    obs = await observe(
        "02:00:00:00:00:01",
        "fire",
        1.5,
        opener=lambda path: Node([VOICE_DOWN]),
        find_all=find_all,
        read=lambda p: DESCRIPTOR,
    )
    assert obs.result == "fire_voice" and obs.nodes == 1


async def test_cancellation_propagates_and_closes_the_transport():
    atvv = Atvv()
    task = asyncio.create_task(
        observe("02:00:00:00:00:01", "atvv", 5, factory=lambda: atvv, find_all=lambda a: [])
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert atvv.closed and task.cancelled()


def test_the_report_dict_has_only_counts_codes_and_usages():
    obs = Observation(seconds=20, family="fire", keys={KeyUsage(0x0C, 0x0221): 3}, nodes=1)
    data = obs.to_dict()
    assert data["result"] == "fire_voice" and data["errors"] == []
    assert data["keys"] == [{"page": "0x000c", "usage": "0x0221", "presses": 3}]
    assert set(RESULTS) >= {data["result"]}


async def test_an_atvv_remote_that_wakes_during_the_test_is_still_observed():
    class Wakes(Atvv):
        opens = 0

        async def open(self, address, on_control, on_audio):
            Wakes.opens += 1
            if Wakes.opens < 3:
                raise ConnectionError("wake_remote")
            return await super().open(address, on_control, on_audio)

    import custom_components.bt_remote_voice.observe as observe_module

    original = observe_module.WAKE_RETRY_SECONDS
    observe_module.WAKE_RETRY_SECONDS = 0.05
    try:
        obs = await run("atvv", atvv=Wakes([bytes([4, 3, 2, 1])]), nodes=(), seconds=1.5)
    finally:
        observe_module.WAKE_RETRY_SECONDS = original
    assert obs.result == "atvv_hold" and Wakes.opens == 3


async def test_an_unreadable_descriptor_is_reported_and_reports_are_only_counted():
    obs = await observe(
        "02:00:00:00:00:01",
        "fire",
        0.6,
        opener=lambda path: Node([VOICE_DOWN, RELEASE]),
        find_all=lambda a: ["/dev/hidraw3"],
        read=lambda p: None,
    )
    assert obs.errors == ["descriptor_unreadable"]
    assert obs.keys == {} and obs.raw_reports == 2 and obs.result == "plain_key"
    assert obs.to_dict()["raw_reports"] == 2


async def test_descriptors_are_kept_per_node_for_the_report_and_bounded():
    obs = await run("fire", nodes=tuple(f"/dev/hidraw{i}" for i in range(7)), node=Node([]))
    assert len(obs.descriptors) == 4 and all(
        v == DESCRIPTOR.hex() for v in obs.descriptors.values()
    )


async def test_distinct_keys_are_capped():
    reports = [bytes([2, usage & 255, usage >> 8, 0, 0]) for usage in range(0x100, 0x100 + 200)]
    obs = await run("fire", node=Node(reports))
    assert len(obs.keys) == 32


async def test_one_of_two_nodes_vanishing_mid_test_keeps_counting_the_other():
    class Vanishes(Node):
        async def read(self):
            raise OSError(19, "gone")

    nodes = {"/dev/hidraw3": Vanishes([]), "/dev/hidraw4": Node([VOICE_DOWN, RELEASE])}
    obs = await observe(
        "02:00:00:00:00:01",
        "fire",
        0.6,
        opener=lambda path: nodes[path],
        find_all=lambda a: list(nodes),
        read=lambda p: DESCRIPTOR,
    )
    # reading from a node that disappeared means the remote disconnected, not that no node existed
    assert obs.errors == ["remote_disconnected"] and obs.keys == {KeyUsage(0x0C, 0x0221): 1}


async def test_a_capability_timeout_and_a_failing_node_lookup_get_accurate_codes():
    obs = await run("atvv", atvv=Atvv(error=TimeoutError()), nodes=())
    assert "bluetooth_operation_failed" in obs.errors
    obs = await observe(
        "02:00:00:00:00:01",
        "fire",
        0.3,
        find_all=lambda a: (_ for _ in ()).throw(OSError("sysfs")),
    )
    assert obs.errors == ["hidraw_unavailable"]


async def test_the_default_observation_opener_opens_the_node_read_only(monkeypatch):
    import os

    import custom_components.bt_remote_voice.fire as fire_module
    import custom_components.bt_remote_voice.observe as observe_module

    flags = []
    monkeypatch.setattr(fire_module.os, "open", lambda path, mode: flags.append(mode) or 99)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_reader", lambda *args: None)
    device = observe_module.observe.__kwdefaults__["opener"]("/dev/hidraw0")
    assert flags[0] & os.O_ACCMODE == os.O_RDONLY
    fire_module.HidrawDevice("/dev/hidraw0")  # the transport's own opener still reads and writes
    assert flags[1] & os.O_ACCMODE == os.O_RDWR
    del device


async def test_a_remote_whose_gatt_service_is_still_appearing_is_retried():
    class Appears(Atvv):
        opens = 0

        async def open(self, address, on_control, on_audio):
            Appears.opens += 1
            if Appears.opens < 3:
                raise ValueError("voice_service_unavailable")
            return await super().open(address, on_control, on_audio)

    import custom_components.bt_remote_voice.observe as observe_module

    original = observe_module.WAKE_RETRY_SECONDS
    observe_module.WAKE_RETRY_SECONDS = 0.05
    try:
        obs = await run("atvv", atvv=Appears([bytes([8])]), nodes=(), seconds=1.5)
    finally:
        observe_module.WAKE_RETRY_SECONDS = original
    assert (
        obs.result == "atvv_tap"
        and Appears.opens == 3
        and "voice_service_unavailable" not in obs.errors
    )


async def test_a_connect_attempt_stuck_past_the_deadline_cannot_hang_the_test():
    class Stuck(Atvv):
        async def open(self, address, on_control, on_audio):
            await asyncio.sleep(60)

    import custom_components.bt_remote_voice.observe as observe_module

    original = observe_module.ATVV_GRACE_SECONDS
    observe_module.ATVV_GRACE_SECONDS = 0.1
    try:
        obs = await asyncio.wait_for(run("atvv", atvv=Stuck(), nodes=(), seconds=0.3), 5)
    finally:
        observe_module.ATVV_GRACE_SECONDS = original
    assert "bluetooth_operation_failed" in obs.errors


async def test_the_timeline_orders_presses_with_times_and_is_capped():
    obs = await run("fire", node=Node([VOICE_DOWN, RELEASE, ARROW_UP, RELEASE]))
    events = [(e["event"], e.get("detail")) for e in obs.timeline]
    assert events[0][0] == "node_found"
    assert events[1:] == [
        ("key_down", "0x000c/0x0221"),
        ("key_up", "0x000c/0x0221"),
        ("key_down", "0x0007/0x0052"),
        ("key_up", "0x0007/0x0052"),
    ]
    times = [e["t"] for e in obs.timeline]
    assert times == sorted(times) and all(isinstance(t, float) for t in times)
    assert obs.to_dict()["timeline"] == obs.timeline

    flood = []
    for usage in range(0x100, 0x100 + 100):
        flood += [bytes([2, usage & 255, usage >> 8, 0, 0]), RELEASE]
    capped = await run("fire", node=Node(flood))
    assert len(capped.timeline) == 40


async def test_a_disconnect_during_the_test_is_a_timeline_event():
    class Drops(Node):
        async def read(self):
            if self.queue.empty():
                raise OSError(19, "gone")
            return await super().read()

    obs = await run("fire", node=Drops([VOICE_DOWN, RELEASE]))
    assert obs.errors == ["remote_disconnected"]
    assert obs.timeline[-1]["event"] == "disconnected"


async def test_atvv_events_appear_on_the_timeline():
    obs = await run("atvv", atvv=Atvv([bytes([4, 3, 2, 1]), bytes([8])]), nodes=())
    assert [(e["event"], e.get("detail")) for e in obs.timeline] == [
        ("atvv_start", "reason 3"),
        ("atvv_search", None),
    ]
