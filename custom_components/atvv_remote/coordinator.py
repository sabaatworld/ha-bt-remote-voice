"""One voice owner per configured remote; no pairing or HID-device management."""

import asyncio
from contextlib import suppress

from .bluez import BlueZTransport
from .protocol import parse_control
from .session import AudioFailure, VoiceSession


class VoiceCoordinator:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.transport = None
        self.caps = None
        self.session = None
        self.stream_id = None
        self.pipeline_task = None
        self.connection_task = None
        self.run_pipeline = None
        self.listeners = set()
        self.event_listeners = set()
        self.mic_active = False
        self.ready = False
        self.last_error = None
        self.stopping = False
        self.ignored_press = False
        self.sessions = 0
        self.failures = 0
        self.pipeline_timeout = 90

    def changed(self):
        for listener in tuple(self.listeners):
            listener()

    def event(self, kind):
        for listener in tuple(self.event_listeners):
            listener(kind)

    def start(self):
        self.connection_task = self.create_task(self.connect_loop(), "ATVV connection")

    def create_task(self, coroutine, name):
        if hasattr(self.entry, "async_create_background_task"):
            return self.entry.async_create_background_task(self.hass, coroutine, name)
        return self.hass.async_create_task(coroutine, name)

    async def close_microphone(self):
        if self.transport and self.stream_id is not None:
            try:
                await asyncio.wait_for(self.transport.write(bytes([13, self.stream_id])), 5)
                self.mic_active = False
                self.changed()
            except Exception, asyncio.CancelledError:
                pass

    async def connect_loop(self):
        delay = 2
        while not self.stopping:
            self.transport = BlueZTransport()
            try:
                self.caps = await self.transport.open(
                    self.entry.data["address"], self.control, self.audio
                )
                self.ready = True
                self.last_error = None
                self.changed()
                delay = 2
                while not self.stopping:
                    try:
                        await asyncio.wait_for(self.transport.lost.wait(), 5)
                        break
                    except TimeoutError:
                        if not await self.transport.check_connected():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.last_error = (
                    str(err)
                    if isinstance(err, (ValueError, ConnectionError))
                    else "bluetooth_unavailable"
                )
            finally:
                self.ready = False
                self.caps = None
                if self.session and not self.session.finished:
                    self.session.abort("disconnected")
                self.mic_active = False
                self.changed()
                await self.transport.close()
            if not self.stopping:
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    def control(self, data):
        event = parse_control(data)
        if event is None:
            # Known malformed start/stop/sync is fatal while recording; unknown
            # vendor/control messages are not treated as microphone commands.
            if (
                data[:1] in (b"\x04", b"\x00", b"\x0a")
                and self.session
                and not self.session.finished
            ):
                self.session.abort("invalid_control")
            return
        if event.kind == "start":
            if self.ignored_press:
                return
            if event.reason != 3 or event.codec != 2:
                if self.session and not self.session.finished:
                    self.session.abort("unsupported_audio_start")
                return
            if (
                self.session
                and not self.session.released
                and not self.session.finished
                and event.stream_id == self.stream_id
            ):
                return
            self.mic_active = True
            self.event("pressed")
            if (
                (self.pipeline_task and not self.pipeline_task.done())
                or not self.caps
                or not self.run_pipeline
            ):
                self.ignored_press = True
                self.last_error = "busy" if self.caps else "not_ready"
                self.changed()
                return
            self.ignored_press = False
            self.stream_id = event.stream_id
            self.session = VoiceSession(self.caps.frame_size)
            self.sessions += 1
            self.last_error = None
            self.pipeline_task = self.create_task(self.process(self.session), "ATVV utterance")
        elif event.kind == "stop":
            self.mic_active = False
            self.event("released")
            if self.ignored_press:
                self.ignored_press = False
            elif self.session and not self.session.finished:
                if event.reason == 2:
                    self.session.release()
                else:
                    self.session.abort("remote_stopped")
        elif event.kind == "sync" and self.session and not self.ignored_press:
            self.session.sync(event)
        # START_SEARCH may duplicate AUDIO_START. Never make it toggle a hold.
        self.changed()

    def audio(self, data):
        if self.session and not self.ignored_press:
            self.session.audio(data)

    async def process(self, session):
        pipeline = self.create_task(self.run_pipeline(session), "ATVV pipeline")
        failed = asyncio.create_task(session.failed.wait())
        try:
            done, _ = await asyncio.wait(
                (pipeline, failed),
                timeout=self.pipeline_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failed in done:
                # Close capture immediately even if the provider is not reading.
                if not session.released:
                    await self.close_microphone()
                raise AudioFailure(session.error or "audio_failed")
            if not done:
                raise AudioFailure("pipeline_timeout")
            await pipeline
            if session.error:
                raise AudioFailure(session.error)
        except asyncio.CancelledError:
            session.abort("cancelled")
            raise
        except Exception as err:
            self.failures += 1
            self.last_error = str(err) if isinstance(err, AudioFailure) else "pipeline_failed"
        finally:
            failed.cancel()
            if not session.finished:
                session.abort("pipeline_finished_early")
            if session.error and not session.released and self.mic_active:
                await self.close_microphone()
            if not pipeline.done():
                pipeline.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(pipeline, 5)
            with suppress(asyncio.CancelledError):
                await failed
            self.changed()

    async def stop(self):
        self.stopping = True
        if self.session and not self.session.released:
            await self.close_microphone()
        if self.session:
            self.session.abort("unloaded")
        for task in (self.connection_task, self.pipeline_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self.transport:
            await self.transport.close()
        self.mic_active = self.ready = False
        self.changed()
