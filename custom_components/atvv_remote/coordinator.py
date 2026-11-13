"""One voice owner per configured remote; no pairing or HID-device management."""

import asyncio
from contextlib import suppress
from dataclasses import replace

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
        self.pipeline_timeout = 150
        self.keepalive_interval = 2
        self.open_grace = 0.15
        self.command_tasks = set()
        self.ignored_timer = None
        self.pending_open = False
        self.open_task = None
        self.open_timer = None
        self.reconnects = 0
        self.last_session = {}

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
        if self.transport and self.caps and self.stream_id is not None:
            try:
                await asyncio.wait_for(
                    self.transport.write(self.caps.close_command(self.stream_id)), 5
                )
                self.mic_active = False
                self.changed()
            except Exception, asyncio.CancelledError:
                pass

    async def connect_loop(self):
        delay = 2
        while not self.stopping:
            self.transport = transport = BlueZTransport()
            try:
                self.caps = await self.transport.open(
                    self.entry.data["address"],
                    lambda data, t=transport: self.control(data) if self.transport is t else None,
                    lambda data, t=transport: self.audio(data) if self.transport is t else None,
                )
                self.reconnects += 1
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
                self.ignored_press = False
                if self.ignored_timer:
                    self.ignored_timer.cancel()
                await self.cancel_commands()
                self.pending_open = False
                if self.open_timer:
                    self.open_timer.cancel()
                if self.open_task and not self.open_task.done():
                    self.open_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.open_task
                if self.session and not self.session.input_complete:
                    self.session.abort("disconnected")
                self.mic_active = False
                # Keep old transport/capabilities until its pipeline has stopped.
                if (
                    self.pipeline_task
                    and not self.pipeline_task.done()
                    and (self.stopping or not self.session or not self.session.input_complete)
                ):
                    self.pipeline_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.pipeline_task
                await transport.close()
                self.caps = None
                self.stream_id = None
                self.changed()
            if not self.stopping:
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    def control(self, data):
        event = parse_control(data, self.caps)
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
        if event.kind == "search":
            # Standard voice button notification. Never open a microphone on a
            # timer/service or on ordinary HID keys. HTT starts itself.
            if (
                self.caps
                and not self.pending_open
                and not self.ignored_press
                and not self.mic_active
                and not (self.pipeline_task and not self.pipeline_task.done())
            ):
                self.pending_open = True
                self.stream_id = 0
                self.open_task = self.create_task(self.request_microphone(), "ATVV voice request")
            return
        if event.kind == "open_error":
            self.pending_open = False
            if self.open_timer:
                self.open_timer.cancel()
            self.last_error = "microphone_open_rejected"
            if self.session and not self.session.finished:
                self.session.abort(self.last_error)
            self.changed()
            return
        if event.kind == "start":
            if self.ignored_press:
                return
            if (
                not self.caps
                or event.codec not in (1, 2)
                or event.reason not in (0, 1, 3)
                or (
                    event.reason == 0
                    and not self.pending_open
                    and not (
                        self.session
                        and not self.session.released
                        and not self.session.finished
                        and event.stream_id == self.stream_id
                    )
                )
            ):
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
            self.pending_open = False
            if self.open_timer:
                self.open_timer.cancel()
            self.event("pressed")
            if (self.pipeline_task and not self.pipeline_task.done()) or not self.run_pipeline:
                self.ignored_press = True
                self.last_error = "busy"
                # Reject this stream, not the previous session's stream. Capture
                # transport/caps/ID now so later retries cannot redirect the write.
                task = self.create_task(
                    self.reject_stream(self.transport, self.caps, event.stream_id),
                    "ATVV busy capture",
                )
                self.command_tasks.add(task)
                task.add_done_callback(self.command_tasks.discard)
                if self.ignored_timer:
                    self.ignored_timer.cancel()
                self.ignored_timer = asyncio.get_running_loop().call_later(3, self.clear_ignored)
                self.changed()
                return
            self.mic_active = True
            self.ignored_press = False
            self.stream_id = event.stream_id
            self.session = VoiceSession(
                self.caps.frame_size, caps=replace(self.caps, codec=event.codec, model=event.reason)
            )
            self.sessions += 1
            self.last_error = None
            self.pipeline_task = self.create_task(self.process(self.session), "ATVV utterance")
        elif event.kind == "stop":
            self.mic_active = False
            self.event("released")
            if self.ignored_press:
                self.ignored_press = False
            elif self.session and not self.session.finished:
                if event.reason in (0, 2):
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
        microphone = self.create_task(self.maintain_microphone(session), "ATVV microphone")
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
            if session.released and self.mic_active:
                await self.close_microphone()
            microphone.cancel()
            with suppress(asyncio.CancelledError):
                await microphone
            failed.cancel()
            self.last_session = {
                "frames": session.frames,
                "first_audio_ms": session.first_audio_ms,
                "elapsed_ms": round((session.loop.time() - session.started_at) * 1000, 1),
                "sequence_mismatch": session.sequence_mismatch,
            }
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

    async def request_microphone(self):
        try:
            # Some devices start themselves after SEARCH. Give their START a
            # brief chance to arrive so this never toggles an existing capture.
            if self.caps.version != 4:
                await asyncio.sleep(self.open_grace)
            if not self.pending_open or self.mic_active:
                return
            await self.transport.write(self.caps.open_command())
            if self.pending_open:
                self.open_timer = asyncio.get_running_loop().call_later(3, self.open_expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.pending_open = False
            self.last_error = "microphone_open_failed"
            self.changed()

    def open_expired(self):
        self.pending_open = False
        self.last_error = "microphone_start_timeout"
        self.changed()

    async def maintain_microphone(self, session):
        next_keepalive = asyncio.get_running_loop().time() + self.keepalive_interval
        while not session.error:
            if session.released:
                if self.mic_active:
                    await self.close_microphone()
                return
            await asyncio.sleep(0.05)
            if session.error or session.released or session is not self.session:
                continue
            if (
                self.transport
                and self.caps
                and self.stream_id is not None
                and asyncio.get_running_loop().time() >= next_keepalive
            ):
                try:
                    await self.transport.write(self.caps.keepalive_command(self.stream_id))
                except Exception:
                    session.abort("microphone_keepalive_failed")
                    return
                next_keepalive = asyncio.get_running_loop().time() + self.keepalive_interval

    def clear_ignored(self):
        self.ignored_press = False

    async def reject_stream(self, transport, caps, stream_id):
        if transport:
            try:
                await asyncio.wait_for(transport.write(caps.close_command(stream_id)), 5)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "microphone_close_failed"
                self.changed()

    async def cancel_commands(self):
        for task in tuple(self.command_tasks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.command_tasks.clear()

    async def stop(self):
        self.stopping = True
        if self.ignored_timer:
            self.ignored_timer.cancel()
        await self.cancel_commands()
        if self.open_timer:
            self.open_timer.cancel()
        if self.open_task:
            self.open_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.open_task
        self.pending_open = False
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
