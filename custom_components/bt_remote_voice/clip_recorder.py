"""Record what a remote sends during "Test my remote": one clip per voice press.

It uses the real transports and the same ``VoiceSession`` decode as a live request (no pipeline),
so a clip is exactly the audio that would go to speech-to-text. The capture rules mirror
``VoiceCoordinator.control``/``accept_capture``; only the pipeline and the entities are left out.
"""

import asyncio
import logging
import time
from dataclasses import replace

from homeassistant.util import dt as dt_util

from .bluez import BlueZTransport
from .clips import MAX_CLIPS, Clip
from .fire import FireTransport
from .observe import KNOWN_ERRORS
from .protocol import parse_control
from .session import VoiceSession

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 2
OPEN_GRACE_SECONDS = 0.15
RETRY_SECONDS = 2
RETRY_ERRORS = {"wake_remote", "voice_service_unavailable"}  # a remote that is still waking


class _Recorder:
    def __init__(self, loop, events, settings):
        self.loop = loop
        self.settings = settings  # ignore_early_stop and max_gap_ms, as for a real request
        self.sessions = []  # every press's session, for the early-stop and gap counts
        self.last_end = None
        self.events = (
            events  # (monotonic time, kind, reason, codec) of search/start, for the test result
        )
        self.caps = self.transport = None
        self.clips = []
        self.pending_open = False
        self.press = None  # SimpleNamespace-like dict of the press in progress
        self.tasks = set()

    def _spawn(self, coroutine):
        task = self.loop.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _live(self, stream_id=None):
        press = self.press
        return (
            press is not None
            and not press["session"].released
            and not press["session"].finished
            and (stream_id is None or press["stream_id"] == stream_id)
        )

    def control(self, data):
        event = parse_control(data, self.caps)
        if event is None or self.caps is None:
            return
        _LOGGER.debug("Test recording: control %s %s", event.kind, data.hex())
        if event.kind in ("search", "start") and self.events is not None and len(self.events) < 20:
            self.events.append(
                (
                    time.monotonic(),
                    event.kind,
                    event.reason if event.kind == "start" else None,
                    event.codec,
                )
            )
        if event.kind == "search":
            if not self.pending_open and not self._live():
                self.pending_open = True
                self._spawn(self._open_microphone())
        elif event.kind == "start":
            self._start(event)
        elif event.kind == "stop":
            press = self.press
            if press and not press["session"].finished:
                if event.reason in (0, 2):
                    press["session"].remote_stop()  # honours "Ignore early stop" like a request
                else:
                    press["session"].abort("remote_stopped")
        elif event.kind == "sync" and self.press:
            self.press["session"].sync(event)

    def audio(self, data):
        if self.press:
            self.press["session"].audio(data)
        elif self.sessions and self.last_end is not None and time.monotonic() - self.last_end < 3:
            # Audio after the press ended: the session counts it (an early STOP that kept streaming).
            self.sessions[-1].audio(data)

    async def _open_microphone(self):
        try:
            if self.caps.version != 4:
                await asyncio.sleep(OPEN_GRACE_SECONDS)
            if self.pending_open and not self._live():
                await self.transport.write(self.caps.open_command())
        except Exception:  # noqa: BLE001 - a failed open just means no clip
            self.pending_open = False

    def _start(self, event):
        valid_codec = (3,) if self.caps.family == "fire" else (1, 2)
        if event.codec not in valid_codec or event.reason not in (0, 1, 3):
            return
        if event.reason == 0 and not self.pending_open and not self._live(event.stream_id):
            return
        if self._live(event.stream_id):
            return  # a repeated start for the press already recording
        self.pending_open = False
        if self.press:
            self._finalize()
        caps = replace(self.caps, codec=event.codec, model=event.reason)
        chunks = bytearray()
        session = VoiceSession(
            caps.frame_size,
            caps=caps,
            max_bytes=1 << 22,
            max_duration=30,
            on_pcm=chunks.extend,
            ignore_early_stop=self.settings["ignore_early_stop"],
            max_gap_ms=self.settings["max_gap_ms"],
        )
        self.sessions.append(session)
        self.press = {
            "session": session,
            "caps": caps,
            "stream_id": event.stream_id,
            "pcm": chunks,
            "started": dt_util.now(),
            "closed": False,
        }
        self._spawn(self._watch(self.press))

    async def _watch(self, press):
        """Keep an ATVV microphone alive, then close it and keep the clip when the press ends."""
        session, caps = press["session"], press["caps"]
        next_keepalive = self.loop.time() + KEEPALIVE_SECONDS
        while not session.finished and not session.error:
            await asyncio.sleep(0.02)
            if self.loop.time() >= next_keepalive and caps.family != "fire":
                next_keepalive = self.loop.time() + KEEPALIVE_SECONDS
                try:
                    await self.transport.write(caps.keepalive_command(press["stream_id"]))
                except Exception:  # noqa: BLE001 - the clip so far is still worth keeping
                    break
        await self.close_microphone(press)
        if self.press is press:
            self._finalize()

    async def close_microphone(self, press=None):
        press = press or self.press
        if press is None or press["closed"]:
            return
        press["closed"] = True
        try:
            await self.transport.write(press["caps"].close_command(press["stream_id"]))
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    def _finalize(self):
        press, self.press = self.press, None
        if press is None:
            return
        self.last_end = time.monotonic()
        session = press["session"]
        ended = session.error or (
            "released" if session.released else "speech_end" if session.finished else "cut"
        )
        _LOGGER.debug(
            "Test recording: press ended (%s) after %s frames, %s bytes of audio",
            ended,
            session.frames,
            len(press["pcm"]),
        )
        if press["pcm"] and len(self.clips) < MAX_CLIPS:
            self.clips.append(
                Clip(
                    started=press["started"],
                    pcm=bytes(press["pcm"]),
                    frames=session.frames,
                    ended=ended,
                    reason=press["caps"].model,
                )
            )


async def _open(transport, address, recorder, deadline, loop):
    """Open the transport, retrying while a sleeping remote wakes up."""
    while True:
        try:
            return await transport.open(address, recorder.control, recorder.audio)
        except (ConnectionError, ValueError) as err:
            if str(err) not in RETRY_ERRORS or loop.time() >= deadline:
                raise
            await transport.close()
            await asyncio.sleep(min(RETRY_SECONDS, max(0, deadline - loop.time())))


async def record_test(
    address,
    family,
    seconds,
    *,
    min_press_seconds=0.0,
    factory=BlueZTransport,
    fire_factory=None,
    events=None,
    errors=None,
    ignore_early_stop=False,
    early_stop_applies=True,
    max_gap_ms=0,
    summary=None,
):
    """Listen for ``seconds`` and return one ``Clip`` per voice press. Never raises on errors.

    The test follows the same settings as a real request: ``ignore_early_stop`` and ``max_gap_ms``
    (``early_stop_applies`` False means early stops are not counted: Fire remotes, conversation mode).
    ``events`` collects search/start events, ``errors`` stable error codes, and ``summary`` receives
    ``early_stops``, ``early_stops_ignored``, ``gaps``, ``gaps_covered`` and ``lost_frames`` (all optional, for the test result). Cancellation
    still cleans up (microphone closed, transport closed) and propagates.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    recorder = _Recorder(
        loop, events, {"ignore_early_stop": ignore_early_stop, "max_gap_ms": max_gap_ms}
    )
    if family == "fire":
        make = fire_factory or (lambda: FireTransport(min_press_seconds=min_press_seconds))
    else:
        make = factory
    recorder.transport = transport = make()
    try:
        recorder.caps = await _open(transport, address, recorder, deadline, loop)
        await asyncio.sleep(max(0, deadline - loop.time()))
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - a remote that cannot be recorded is a result, not a crash
        if errors is not None:
            code = str(err)
            if code not in KNOWN_ERRORS:
                known = (ConnectionError, ValueError, TimeoutError)
                code = (
                    "bluetooth_operation_failed"
                    if isinstance(err, known)
                    else "bluetooth_unavailable"
                )
            errors.append(code)
    finally:
        try:
            if recorder.press:
                await recorder.close_microphone()
                recorder._finalize()
        finally:
            for item in recorder.sessions:
                if not item.finished:
                    item.abort("cancelled")  # stops its watchdog and limit timers
        for task in tuple(recorder.tasks):
            task.cancel()
        try:
            await transport.close()
        except Exception:  # noqa: BLE001 - closing must not raise
            pass
    if summary is not None:
        seen = [item for item in recorder.sessions if early_stop_applies and item.early_stop_seen]
        summary["early_stops"] = len(seen)  # every early stop the remote sent
        summary["early_stops_ignored"] = sum(1 for item in seen if item.ignore_early_stop)
        summary["gaps"] = sum(item.gaps for item in recorder.sessions)
        summary["gaps_covered"] = sum(item.gaps_covered for item in recorder.sessions)
        summary["lost_frames"] = sum(item.lost_frames for item in recorder.sessions)
    return recorder.clips
