"""Listen-only observation of a remote during setup ("Test my remote").

It never opens a microphone and never writes to a remote's input device. ATVV remotes are
observed through the standard control channel the integration already uses at setup (including
its capability request); every remote's keys are read from its kernel HID node. Only the usages
of keys pressed during the test are counted; nothing is stored beyond the report.
"""

import asyncio
import time
from dataclasses import dataclass, field

from .bluez import BlueZTransport
from .fire import HidrawDevice, find_all_hidraw, read_descriptor
from .hid_descriptor import CONSUMER_PAGE, KeyUsage, decode_keys, parse_reports
from .protocol import parse_control

OBSERVE_SECONDS = 20
VOICE_KEY = KeyUsage(CONSUMER_PAGE, 0x0221)  # consumer "voice command"
MAX_EVENTS = 20
MAX_TIMELINE = 40
MAX_KEYS = 32  # distinct key usages recorded; a mistaken keyboard cannot flood the report
MAX_DESCRIPTORS = 4
WAKE_RETRY_SECONDS = 2
ATVV_GRACE_SECONDS = 6  # an attempt in flight at the deadline may finish its own bounded calls
RETRY_ERRORS = {"wake_remote", "voice_service_unavailable"}  # a remote that is still waking
KNOWN_ERRORS = {
    "wake_remote",
    "bluetooth_unavailable",
    "bluetooth_operation_failed",
    "not_paired",
    "multiple_adapters",
    "voice_service_unavailable",
    "unsupported_capabilities",
    "unsupported_codec",
    "invalid_frame_size",
    "unsupported_write",
}

# Stable outcome names; each has a translated result screen in config_flow.
RESULTS = ("atvv_hold", "atvv_tap", "fire_voice", "plain_key", "nothing")


@dataclass
class Observation:
    seconds: float = 0
    family: str = "unknown"
    atvv_events: list = field(default_factory=list)  # (kind, reason, codec)
    audio_frames: int = 0
    keys: dict = field(default_factory=dict)  # KeyUsage -> presses
    raw_reports: int = 0  # input reports that arrived but could not be decoded (no descriptor)
    descriptors: dict = field(default_factory=dict)  # node -> descriptor hex, for the report
    nodes: int | None = None
    errors: list = field(default_factory=list)  # stable codes only
    started: float = 0.0  # monotonic start, for the timeline
    timeline: list = field(default_factory=list)  # [{"t": seconds, "event": ..., "detail": ...}]

    @property
    def result(self):
        """What the remote showed: the single outcome the setup screen reports."""
        for kind, reason, _codec in self.atvv_events:
            if kind == "start" and reason == 3:
                return "atvv_hold"
        if any(kind in ("search", "start") for kind, _r, _c in self.atvv_events):
            return "atvv_tap"
        if self.family == "fire" and VOICE_KEY in self.keys:
            return "fire_voice"
        if self.keys or self.raw_reports:
            return "plain_key"
        return "nothing"

    def to_dict(self):
        return {
            "result": self.result,
            "seconds": self.seconds,
            "family": self.family,
            "atvv_events": [{"kind": k, "reason": r, "codec": c} for k, r, c in self.atvv_events],
            "audio_frames": self.audio_frames,
            "keys": [
                {"page": f"0x{key.page:04x}", "usage": f"0x{key.usage:04x}", "presses": n}
                for key, n in sorted(self.keys.items(), key=lambda kv: (kv[0].page, kv[0].usage))
            ],
            "raw_reports": self.raw_reports,
            "hidraw_nodes": self.nodes,
            "errors": list(self.errors),
            "timeline": list(self.timeline),
        }


def _mark(obs, event, detail=None):
    """Append a timestamped event (seconds since the test began), bounded."""
    if len(obs.timeline) < MAX_TIMELINE:
        entry = {"t": round(time.monotonic() - obs.started, 1), "event": event}
        if detail:
            entry["detail"] = detail
        obs.timeline.append(entry)


def _usage_name(key):
    return f"0x{key.page:04x}/0x{key.usage:04x}"


def _error(obs, code):
    if code not in obs.errors:
        obs.errors.append(code)


async def _watch_node(node, deadline, obs, opener, read):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, read, node)
    if data:
        if len(obs.descriptors) < MAX_DESCRIPTORS:
            obs.descriptors[node] = bytes(data).hex()
    else:
        _error(obs, "descriptor_unreadable")
    reports = parse_reports(data) if data else ()
    try:
        device = opener(node)
    except PermissionError:
        _error(obs, "hidraw_permission_denied")
        return
    except OSError:
        _error(obs, "hidraw_unavailable")
        return
    held = frozenset()
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                report = await asyncio.wait_for(device.read(), remaining)
            except TimeoutError:
                return
            except OSError:
                # The node disappeared while reading: the remote disconnected (it sleeps when idle).
                _error(obs, "remote_disconnected")
                _mark(obs, "disconnected")
                return
            if not reports:  # cannot be decoded without a descriptor: count, never keep
                obs.raw_reports += 1
                continue
            keys = decode_keys(reports, report)
            for key in sorted(keys - held, key=lambda k: (k.page, k.usage)):  # presses, not repeats
                _mark(obs, "key_down", _usage_name(key))
                if key in obs.keys or len(obs.keys) < MAX_KEYS:
                    obs.keys[key] = obs.keys.get(key, 0) + 1
            for key in sorted(held - keys, key=lambda k: (k.page, k.usage)):
                _mark(obs, "key_up", _usage_name(key))
            held = keys
    finally:
        device.close()


async def _watch_hid(address, deadline, obs, opener, find_all, read):
    loop = asyncio.get_running_loop()
    try:
        nodes = await loop.run_in_executor(None, find_all, address)
        while not nodes and time.monotonic() < deadline:  # the node appears once the remote wakes
            await asyncio.sleep(0.5)
            nodes = await loop.run_in_executor(None, find_all, address)
    except OSError:
        _error(obs, "hidraw_unavailable")
        return
    obs.nodes = len(nodes)
    if not nodes:
        _error(obs, "no_hidraw_node")
        return
    _mark(obs, "node_found", f"{len(nodes)} node(s)")
    await asyncio.gather(*(_watch_node(n, deadline, obs, opener, read) for n in nodes))


async def _watch_atvv(address, deadline, obs, factory):
    transport = factory()
    holder = {}

    def on_control(data):
        event = parse_control(data, holder.get("caps"))
        if event and event.kind in ("start", "search") and len(obs.atvv_events) < MAX_EVENTS:
            obs.atvv_events.append((event.kind, event.reason, event.codec))
            _mark(
                obs,
                f"atvv_{event.kind}",
                f"reason {event.reason}" if event.kind == "start" else None,
            )

    def on_audio(data):
        obs.audio_frames += 1

    try:
        while True:  # a sleeping remote is not connected yet: keep trying until it wakes
            try:
                holder["caps"] = await transport.open(address, on_control, on_audio)
                break
            except (ConnectionError, ValueError) as err:
                if str(err) not in RETRY_ERRORS or time.monotonic() >= deadline:
                    raise
                await transport.close()
                await asyncio.sleep(min(WAKE_RETRY_SECONDS, max(0, deadline - time.monotonic())))
        await asyncio.sleep(max(0, deadline - time.monotonic()))
    except (ConnectionError, ValueError) as err:
        _error(obs, str(err) if str(err) in KNOWN_ERRORS else "bluetooth_operation_failed")
    except TimeoutError:
        _error(obs, "bluetooth_operation_failed")
    except Exception:  # noqa: BLE001 - a failed listen is a result, not a crash
        _error(obs, "bluetooth_unavailable")
    finally:
        try:
            await transport.close()
        except Exception:  # noqa: BLE001 - closing must not raise
            pass


async def observe(
    address,
    family,
    seconds=OBSERVE_SECONDS,
    *,
    factory=BlueZTransport,
    opener=lambda path: HidrawDevice(path, readonly=True),
    find_all=find_all_hidraw,
    read=read_descriptor,
):
    """Listen for ``seconds`` and return an ``Observation``. Never writes; never raises."""
    obs = Observation(seconds=seconds, family=family, started=time.monotonic())
    deadline = obs.started + seconds
    watchers = [_watch_hid(address, deadline, obs, opener, find_all, read)]
    if family != "fire":
        watchers.append(
            asyncio.wait_for(
                _watch_atvv(address, deadline, obs, factory),
                max(0, deadline - time.monotonic()) + ATVV_GRACE_SECONDS,
            )
        )
    for outcome in await asyncio.gather(*watchers, return_exceptions=True):
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome  # cancellation must propagate
        if isinstance(outcome, TimeoutError):
            _error(obs, "bluetooth_operation_failed")
        elif isinstance(outcome, Exception):
            _error(obs, "bluetooth_unavailable")
    return obs
