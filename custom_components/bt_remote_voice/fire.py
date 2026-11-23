"""Amazon Fire TV voice remote: HID-over-GATT voice read through its hidraw node.

BlueZ owns the HID service and refuses GATT writes to it, so the kernel's hidraw
device is the working path (verified on HAOS: Home Assistant's container can open
and write it). The remote sends Opus packets (80 bytes, 16 kHz mono, 20 ms) as
input report 0xF0 while the microphone is enabled with output report 0xF2.

This transport presents the remote to the coordinator through the same callback
interface as the BLE transport: ATVV-shaped control messages (start/stop) and raw
audio packets. Frames arrive in bursts (100-260 ms gaps are normal) and carry no
sequence numbers, so loss cannot be detected the way ATVV sync counters allow.
"""

import asyncio
import errno
import glob
import logging
import os
import time

from .hid_descriptor import find_voice_reports, parse_reports
from .opus import opus_available
from .protocol import Capabilities

_LOGGER = logging.getLogger(__name__)

FIRE_VOICE_SERVICE = "5de20000-5e8d-11e6-8b77-86f30ca893d3"
AMAZON_VENDOR = 0x0171
VOICE_USAGE = 0x0221
REPORT_CONSUMER = 0x02
REPORT_AUDIO = 0xF0
REPORT_MIC = 0xF2
STREAM_ID = 1
HOLD_START = bytes([4, 3, 3, STREAM_ID])  # ATVV-shaped start: reason 3 (hold), codec 3 (Opus)
HOLD_STOP = bytes([0, 0])


def find_hidraw(address, sysfs="/sys/class/hidraw"):
    """Return /dev/hidrawN for the remote with this address, or None (matched case-insensitively)."""
    wanted = address.lower()
    for uevent in sorted(glob.glob(f"{sysfs}/hidraw*/device/uevent")):
        try:
            with open(uevent) as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.lower().startswith("hid_uniq=") and line.split("=", 1)[1].lower() == wanted:
                return "/dev/" + os.path.basename(os.path.dirname(os.path.dirname(uevent)))
    return None


def find_all_hidraw(address, sysfs="/sys/class/hidraw"):
    """Every /dev/hidrawN for the remote with this address (a remote can expose several)."""
    wanted = address.lower()
    nodes = []
    for uevent in sorted(glob.glob(f"{sysfs}/hidraw*/device/uevent")):
        try:
            with open(uevent) as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.lower().startswith("hid_uniq=") and line.split("=", 1)[1].lower() == wanted:
                nodes.append("/dev/" + os.path.basename(os.path.dirname(os.path.dirname(uevent))))
                break
    return nodes


def read_descriptor(node, sysfs="/sys/class/hidraw"):
    """The node's HID report descriptor, or None when it cannot be read."""
    try:
        with open(f"{sysfs}/{os.path.basename(node)}/device/report_descriptor", "rb") as handle:
            return handle.read()
    except OSError:
        return None


_read_descriptor = read_descriptor  # the constructor parameter shadows the name


def parse_report(data, voice_down):
    """Classify one input report: ("voice_down"|"voice_up"|"audio"|"other", payload).

    The consumer report is 4 bytes (two usage slots); the all-zero release is shared by
    every consumer key, so a release only counts as voice when the voice key was down.
    """
    if not data:
        return "other", b""
    if data[0] == REPORT_AUDIO:
        return "audio", data[1:]
    if data[0] == REPORT_CONSUMER and len(data) >= 5:
        slots = (data[1] | data[2] << 8, data[3] | data[4] << 8)
        pressed = VOICE_USAGE in slots
        if pressed and not voice_down:
            return "voice_down", b""
        if voice_down and not pressed:
            return "voice_up", b""
    return "other", b""


class HidrawDevice:
    """One persistent file descriptor on a hidraw node, read through the event loop."""

    def __init__(self, path, readonly=False):
        # readonly: observation only needs to read, and cannot write by construction.
        self._fd = os.open(path, (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NONBLOCK)
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._error = None
        self._loop.add_reader(self._fd, self._ready)

    def _ready(self):
        try:
            data = os.read(self._fd, 256)
        except BlockingIOError:
            return
        except OSError as err:
            self._error = err
            self._loop.remove_reader(self._fd)
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(data)

    async def read(self):
        item = await self._queue.get()
        if item is None:
            raise self._error or OSError(errno.ENODEV, "hidraw closed")
        return item

    def write(self, data):
        os.write(self._fd, data)

    def close(self):
        if self._fd is not None:
            try:
                self._loop.remove_reader(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


class FireTransport:
    def __init__(
        self,
        opener=HidrawDevice,
        find=find_hidraw,
        wait_seconds=10,
        poll_seconds=0.2,
        rearm_seconds=0.5,
        min_press_seconds=0,
        find_all=None,
        read_descriptor=None,
        refuse_after_seconds=2,
    ):
        # Descriptor validation needs find_all/read_descriptor. They default on only with the
        # real sysfs finder (production); a caller that injects its own ``find`` gets the
        # original behavior (first node, fixed report IDs) unless it passes them explicitly.
        if find_all is None and find is find_hidraw:
            find_all, read_descriptor = find_all_hidraw, read_descriptor or _read_descriptor
        self.find_all = find_all
        self.read_descriptor = read_descriptor
        self.refuse_after_seconds = refuse_after_seconds
        self.unsupported_since = None  # first time readable descriptors lacked the voice pair
        self.hidraw_nodes = None
        self.descriptor_reports = None
        self.descriptor_hex = None
        self.opener = opener
        self.find = find
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.rearm_seconds = rearm_seconds
        self.min_press_seconds = min_press_seconds
        self.pending = None  # timer of a voice press that has not started its request yet
        self.ignored_presses = 0
        self.lost = asyncio.Event()
        self.device = None
        self.task = None
        self.on_control = self.on_audio = None
        self.voice_down = False
        self.mic_closed = False
        self.frames = 0
        self.last_frame = self.last_enable = 0.0
        # Counters for support (no audio, no addresses): what the remote did after each press.
        self.stats = {
            "presses": 0,
            "enables": 0,
            "frames": 0,
            "first_frame_ms": None,  # enable -> first audio frame, last press
            "max_gap_ms": 0,  # longest silence between frames inside a press
            "toc": {},  # Opus TOC byte (hex) -> frames, at most 8 distinct
        }
        self._awaiting_first = False

    async def open(self, address, on_control, on_audio):
        if not opus_available():
            raise ValueError("opus_decoder_unavailable")
        # sysfs reads are blocking I/O: keep them off Home Assistant's event loop.
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self.wait_seconds
        path = await loop.run_in_executor(None, self._locate, address)
        while path is None and time.monotonic() < deadline:
            if (
                self.unsupported_since is not None
                and time.monotonic() - self.unsupported_since >= self.refuse_after_seconds
            ):
                break  # nodes exist, none has the voice pair, and none appeared in time
            await asyncio.sleep(self.poll_seconds)
            path = await loop.run_in_executor(None, self._locate, address)
        if path is None:
            if self.unsupported_since is not None:
                raise ValueError("fire_voice_unsupported")
            raise ConnectionError("wake_remote")
        try:
            self.device = self.opener(path)
        except PermissionError:
            raise ValueError("hidraw_permission_denied") from None
        except OSError:
            raise ConnectionError("hidraw_unavailable") from None
        self.on_control, self.on_audio = on_control, on_audio
        self.task = asyncio.create_task(self._reader(), name="BT voice Fire hidraw reader")
        return Capabilities(frame_size=80, version=5, codec=3, model=0, family="fire")

    def _locate(self, address):
        """The hidraw node to use, or None while there is none (blocking sysfs reads).

        With a descriptor reader, the node whose descriptor has the verified voice pair wins.
        Readable descriptors without it return None and start a short grace period (a remote's
        nodes can appear one at a time); ``open`` refuses once it runs out. Unreadable
        descriptors keep the original behavior (first node, fixed report IDs).
        """
        if self.find_all is None:
            return self.find(address)
        nodes = self.find_all(address)
        self.hidraw_nodes = len(nodes)
        if not nodes:
            return None
        readable = False
        for node in nodes:
            data = self.read_descriptor(node) if self.read_descriptor else None
            if not data:  # None (unreadable) or empty: nothing to judge the node by
                continue
            readable = True
            reports = parse_reports(data)
            self.descriptor_reports, self.descriptor_hex = reports, data.hex()
            if find_voice_reports(reports):
                self.unsupported_since = None
                return node
        if readable:
            if self.unsupported_since is None:
                self.unsupported_since = time.monotonic()
            return None
        _LOGGER.debug("Fire remote descriptor unreadable; using the fixed voice report IDs")
        return nodes[0]

    def _write_mic(self, on):
        self.device.write(bytes([REPORT_MIC, 1 if on else 0]))

    def _enable(self, on):
        try:
            self._write_mic(on)
        except OSError:
            self.lost.set()
            return
        if on:
            self.last_enable = time.monotonic()
            self.stats["enables"] += 1
            self._awaiting_first = True
            self.stats["first_frame_ms"] = None
            _LOGGER.debug("Fire remote: microphone enable written")

    async def _reader(self):
        while True:
            try:
                data = await asyncio.wait_for(self.device.read(), 0.1)
            except TimeoutError:
                self._maybe_rearm()
                continue
            except OSError:
                self.lost.set()
                return
            except Exception:  # noqa: BLE001 - a dead reader must read as a lost remote
                _LOGGER.exception("Fire remote reader failed")
                self.lost.set()
                return
            try:
                self._handle(data)
            except Exception:  # noqa: BLE001 - one bad report must not kill the reader
                _LOGGER.exception("Fire remote report handling failed; report dropped")
            self._maybe_rearm()

    def _handle(self, data):
        kind, payload = parse_report(data, self.voice_down)
        if kind == "voice_down":
            self.stats["presses"] += 1
            _LOGGER.debug("Fire remote: voice key down")
            self.voice_down, self.mic_closed, self.frames = True, False, 0
            if self.min_press_seconds > 0:
                # A sleeping remote replays the key that woke it as an instant tap: wait
                # to see whether the key is really held before starting a request.
                self.pending = asyncio.get_running_loop().call_later(
                    self.min_press_seconds, self._begin_hold
                )
            else:
                self._begin_hold()
        elif kind == "voice_up" and self.pending:
            self.pending.cancel()
            self.pending = None
            self.voice_down = False
            self.ignored_presses += 1
            _LOGGER.debug("Fire remote voice press shorter than the minimum was ignored")
        elif kind == "voice_up":
            _LOGGER.debug("Fire remote: voice key up after %s audio frames", self.frames)
            self.voice_down = False
            if not self.mic_closed:
                self._enable(False)
            self.mic_closed = False
            self.on_control(HOLD_STOP)
        elif kind == "audio" and self.voice_down and not self.mic_closed and not self.pending:
            now = time.monotonic()
            self._count_frame(payload, now)
            self.frames += 1
            self.last_frame = now
            self.on_audio(payload)

    def _count_frame(self, payload, now):
        stats = self.stats
        stats["frames"] += 1
        if self._awaiting_first:
            self._awaiting_first = False
            stats["first_frame_ms"] = round((now - self.last_enable) * 1000)
            _LOGGER.debug("Fire remote: first audio frame after %s ms", stats["first_frame_ms"])
        elif self.frames:
            stats["max_gap_ms"] = max(stats["max_gap_ms"], round((now - self.last_frame) * 1000))
        toc = payload[:1].hex()
        if toc in stats["toc"] or len(stats["toc"]) < 8:
            stats["toc"][toc] = stats["toc"].get(toc, 0) + 1

    def _begin_hold(self):
        self.pending = None
        if not self.voice_down:
            return
        try:
            self.on_control(HOLD_START)
            self._enable(True)
        except Exception:  # noqa: BLE001 - a timer callback must not leave a half-started hold
            _LOGGER.exception("Fire remote hold start failed; press dropped")
            self.voice_down = False

    def _maybe_rearm(self):
        # The remote ignores an enable sent before its voice key; re-ask only if frames
        # started and then stopped while the key is still held (never after a close).
        now = time.monotonic()
        if (
            self.voice_down
            and not self.mic_closed
            and self.frames
            and now - self.last_frame > self.rearm_seconds
            and now - self.last_enable >= self.rearm_seconds
        ):
            self._enable(True)

    async def write(self, data):
        """Translate the coordinator's microphone messages (ATVV close/open/keepalive)."""
        if data and data[0] == 0x0D and self.device is not None:  # close
            self.mic_closed = True
            self._enable(False)
        # 0x0C open and 0x0E keepalive: the remote starts and stops itself.

    async def check_connected(self):
        return self.device is not None and not self.lost.is_set()

    async def close(self):
        if self.pending:
            self.pending.cancel()
            self.pending = None
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError, Exception:  # noqa: BLE001 - closing must not raise
                pass
        if self.device is not None:
            self.device.close()
