"""One voice owner per configured remote; no pairing or HID-device management."""

import asyncio
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from .bluez import BlueZTransport
from .held import HeldTurns
from .history import CaptureHistory
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
        self.active_input = None
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
        self.retirement_timeout = 5
        self.retirement_wait = 20
        self.last_timings = {}
        self.keepalive_interval = 2
        self.open_grace = 0.15
        self.command_tasks = set()
        self.ignored_timer = None
        self.pending_open = False
        self.open_task = None
        self.open_timer = None
        self.reconnects = 0
        self.last_session = {}
        self.history = CaptureHistory()
        self.capture = None
        self.pending = None
        self.queue_ttl = 30
        self.promotion_task = None
        self.phase = "idle"
        self.rejected = 0
        self.queued = 0
        self.turns = 0
        self.last_outcome = None
        self.capture_tasks = set()
        self.recovery_blocked = False
        self.retiring_generation = None
        self.last_failure = None

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

    def outcome(self, kind, reason=None):
        from .preferences import error_message

        self.last_outcome = {
            "outcome": kind,
            "stage": self.phase,
            "code": reason,
            "message": error_message(reason),
        }
        if kind == "failed":
            # Latched for the diagnostic sensor until a later request succeeds.
            self.last_failure = {
                "code": reason or "unknown",
                "message": error_message(reason) or "Unknown failure",
                "stage": self.phase,
                "since": dt_util.utcnow().isoformat(),
                "session": self.sessions,
            }
        self.event(kind)
        self.changed()

    async def close_microphone(self, owner=None):
        owner = owner or self.capture
        if owner is None:
            return
        if owner.closed:
            return
        if owner.transport and owner.caps:
            try:
                await asyncio.wait_for(
                    owner.transport.write(owner.caps.close_command(owner.stream_id)), 5
                )
            except Exception:
                self.history.record(owner.record, "close", ok=False)
                self.last_error = "microphone_close_failed"
                return
        owner.closed = True
        self.history.record(owner.record, "close", ok=True)
        if self.capture is owner:
            self.mic_active = False
            self.changed()

    def accept_capture(self, event, *, pending=False):
        self.sessions += 1
        caps = replace(self.caps, codec=event.codec, model=event.reason)
        held = self.entry.options.get("input_mode", "single") == "conversation"
        turns = asyncio.Queue(maxsize=1)
        controller = HeldTurns(turns.put_nowait) if held else None
        session = VoiceSession(
            caps.frame_size,
            caps=caps,
            # Up to 30 s of 16 kHz PCM may wait for a slow consumer to start reading.
            max_bytes=960000,
            max_duration=120 if held else 30,
            on_pcm=controller.feed if controller else None,
        )
        owner = SimpleNamespace(
            generation=self.sessions,
            transport=self.transport,
            caps=caps,
            stream_id=event.stream_id,
            session=session,
            closed=False,
            record=self.history.begin(self.sessions, model=event.reason),
            held=controller,
            turns=turns,
            released_at=None,
            expiry=None,
            release_timer=None,
        )
        self.capture = owner
        self.session = session
        self.stream_id = event.stream_id
        self.mic_active = True
        self.last_error = None
        self.history.record(owner.record, "start")
        if held and event.reason != 3:
            session.abort("held_mode_unsupported")
        return owner

    async def promote(self, owner, old_task, replace_old):
        try:
            if replace_old:
                if not getattr(getattr(self, "satellite", None), "generation_complete", False):
                    self.outcome("context_reset", "active_generation_cancelled")
                old_task.cancel()
            try:
                # Shield prevents timeout from silently cancelling active original work.
                await asyncio.wait_for(
                    asyncio.shield(old_task), self.retirement_timeout if replace_old else 150
                )
            except asyncio.CancelledError:
                if not old_task.done():
                    raise
            except Exception:
                if replace_old:
                    raise AudioFailure("replacement_retirement_failed") from None
            if self.stopping or owner is not self.pending:
                return
            if self.recovery_blocked:
                raise AudioFailure("playback_stop_unverified")
            if owner.session.error:
                raise AudioFailure(owner.session.error)
            if owner.released_at and owner.session.loop.time() - owner.released_at >= 30:
                raise AudioFailure("queue_expired")
            self.pending = None
            if owner.expiry:
                owner.expiry.cancel()
            self.pipeline_task = self.create_task(
                self.process(owner.session, owner), "ATVV promoted"
            )
        except asyncio.CancelledError:
            if not owner.session.finished:
                owner.session.abort("queue_discarded")
            await self.close_microphone(owner)
            raise
        except Exception as err:
            reason = str(err) if isinstance(err, AudioFailure) else "queue_failed"
            owner.session.abort(reason)
            self.failures += 1
            self.last_error = reason
            self.history.finish(owner.record, error=reason)
            self.outcome("failed", reason)
            await self.close_microphone(owner)
        finally:
            if self.pending is owner:
                self.pending = None

    def expire_pending(self, owner, reason="queue_expired"):
        if owner is not self.pending:
            return
        self.pending = None
        owner.session.abort(reason)
        self.failures += 1
        self.last_error = reason
        self.history.finish(owner.record, error=reason)
        self.outcome("failed", reason)
        if self.promotion_task and not self.promotion_task.done():
            self.promotion_task.cancel()

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
                if self.pending:
                    self.expire_pending(self.pending, "disconnected")
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
                # Decide from the input the running pipeline actually owns; the
                # mutable physical self.session may belong to a pending capture or,
                # in held mode, is never drained by the logical request.
                active = self.active_input
                if active and not active.input_complete:
                    active.abort("disconnected")
                physical = self.session
                if physical and not physical.input_complete:
                    held = self.capture and self.capture.session is physical and self.capture.held
                    if not (held and (physical.released or physical.finished)):
                        physical.abort("disconnected")
                self.mic_active = False
                # Keep old transport/capabilities until its pipeline has stopped.
                if (
                    self.pipeline_task
                    and not self.pipeline_task.done()
                    and (self.stopping or not active or not active.input_complete)
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

    def refresh_recovery(self):
        if not self.recovery_blocked:
            return
        if self.retiring_generation and not self.retiring_generation.done():
            return
        if hasattr(self.hass, "states"):
            speaker = self.entry.options.get("speaker", "")
            player = self.hass.states.get(speaker) if speaker else None
            if not speaker or (player and player.state in ("idle", "off", "on")):
                self.recovery_blocked = False
                self.retiring_generation = None

    def control(self, data):
        self.refresh_recovery()
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
                and self.pending is None
                and not self.recovery_blocked
                and (
                    not (self.pipeline_task and not self.pipeline_task.done())
                    or self.entry.options.get(f"busy_{self.phase}", "reject") != "reject"
                )
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
            busy = self.pipeline_task and not self.pipeline_task.done()
            policy = self.entry.options.get(f"busy_{self.phase}", "reject")
            blocked = self.pending is not None or self.recovery_blocked
            if blocked or not self.run_pipeline or (busy and policy == "reject"):
                self.ignored_press = True
                self.rejected += 1
                self.last_error = "queue_full" if self.pending else "busy"
                if self.recovery_blocked:
                    self.last_error = "playback_stop_unverified"
                task = self.create_task(
                    self.reject_stream(self.transport, self.caps, event.stream_id),
                    "ATVV rejected capture",
                )
                self.command_tasks.add(task)
                task.add_done_callback(self.command_tasks.discard)
                if self.ignored_timer:
                    self.ignored_timer.cancel()
                self.ignored_timer = asyncio.get_running_loop().call_later(3, self.clear_ignored)
                self.outcome("rejected", self.last_error)
                return
            self.pending_open = False
            self.ignored_press = False
            owner = self.accept_capture(event, pending=bool(busy))
            if busy:
                self.pending = owner
                self.queued += 1
                self.outcome("queued", "replacement" if policy == "replace" else None)
                task = self.create_task(
                    self.maintain_microphone(owner.session, owner), "ATVV pending mic"
                )
                self.capture_tasks.add(task)
                task.add_done_callback(self.capture_tasks.discard)
                self.promotion_task = self.create_task(
                    self.promote(owner, self.pipeline_task, policy == "replace"),
                    "ATVV pending request",
                )
            else:
                self.outcome("accepted")
                self.pipeline_task = self.create_task(
                    self.process(owner.session, owner), "ATVV utterance"
                )
        elif event.kind == "stop":
            self.mic_active = False
            self.event("released")
            if not self.ignored_press and self.capture:
                self.capture.closed = True
                self.capture.released_at = asyncio.get_running_loop().time()
                self.history.record(self.capture.record, "stop", count=self.capture.session.frames)
                if self.capture.held and event.reason in (0, 2):
                    delay = self.capture.session.finish_delay
                    if delay:
                        self.capture.release_timer = asyncio.get_running_loop().call_later(
                            delay, self.capture.held.release
                        )
                    else:
                        self.capture.held.release()
                elif self.capture.held and self.capture.held.current:
                    self.capture.held.current.abort("remote_stopped")
                    self.capture.held.current = None
                if self.pending is self.capture:
                    self.capture.expiry = asyncio.get_running_loop().call_later(
                        self.queue_ttl, self.expire_pending, self.capture
                    )
            if self.ignored_press:
                self.ignored_press = False
            elif self.session and not self.session.finished:
                if event.reason in (0, 2):
                    self.session.release()
                else:
                    self.session.abort("remote_stopped")
        elif event.kind == "sync" and self.session and not self.ignored_press:
            self.session.sync(event)
            if self.capture:
                self.history.record(
                    self.capture.record,
                    "sync",
                    expected=event.sequence,
                    received=self.session.sequence,
                )
        # START_SEARCH may duplicate AUDIO_START. Never make it toggle a hold.
        self.changed()

    def audio(self, data):
        if self.session and not self.ignored_press:
            self.session.audio(data)
            if self.capture and (self.session.error or self.session.frames % 64 == 0):
                self.history.record(
                    self.capture.record, "rx", count=self.session.frames, reason=self.session.error
                )

    async def process(self, session, owner=None, *, logical=False):
        owner = owner or self.capture
        if owner and owner.held and not logical:
            await self.process_held(owner)
            return
        self.turns += 1
        pipeline = self.create_task(self.run_pipeline(session), "ATVV pipeline")
        microphone = (
            None
            if logical
            else self.create_task(self.maintain_microphone(session, owner), "ATVV microphone")
        )
        failed = asyncio.create_task(session.failed.wait())
        self.active_input = session
        self.phase = "processing"
        succeeded = True
        failure_code = None
        try:
            done, _ = await asyncio.wait(
                (pipeline, failed),
                timeout=self.pipeline_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failed in done:
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
            succeeded = False
            self.failures += 1
            failure_code = str(err) if isinstance(err, AudioFailure) else "pipeline_failed"
            self.last_error = failure_code
            self.outcome("failed", self.last_error)
        finally:
            if not session.finished:
                session.abort("pipeline_finished_early")
            if microphone:
                microphone.cancel()
                with suppress(asyncio.CancelledError):
                    await microphone
            if not logical:
                await self.close_microphone(owner)
            failed.cancel()
            if not pipeline.done():
                pipeline.cancel()
                # The satellite owns generation retirement (its own 5 s bound plus
                # playback stop); never inject a second cancel into that cleanup.
                await asyncio.wait({pipeline}, timeout=self.retirement_wait)
                if not pipeline.done():
                    self.recovery_blocked = True
                    self.retiring_generation = pipeline
                    self.last_error = failure_code = "generation_retirement_failed"
                    succeeded = False
            if pipeline.done() and not pipeline.cancelled():
                err = pipeline.exception()
                if isinstance(err, AudioFailure) and str(err).startswith(
                    ("playback_stop", "generation_retirement")
                ):
                    self.recovery_blocked = True
                    self.last_error = failure_code = str(err)
                    succeeded = False
            with suppress(asyncio.CancelledError):
                await failed
            self.last_session = {
                "frames": session.frames,
                "first_audio_ms": session.first_audio_ms,
                "elapsed_ms": round((session.loop.time() - session.started_at) * 1000, 1),
                "sequence_mismatch": session.sequence_mismatch,
            }
            if owner:
                self.history.record(owner.record, "turn_end", reason=session.error or failure_code)
                self.history.timings(owner.record, self.last_timings)
                if not logical:
                    self.history.finish(owner.record, error=session.error or failure_code)
            if self.active_input is session:
                self.active_input = None
            self.phase = "idle"
            if succeeded and not failure_code and not self.recovery_blocked:
                self.last_failure = None
            self.changed()

        return succeeded and not self.recovery_blocked

    async def process_held(self, owner):
        microphone = self.create_task(
            self.maintain_microphone(owner.session, owner), "ATVV held mic"
        )
        try:
            while True:
                if owner.session.error:
                    raise AudioFailure(owner.session.error)
                if owner.session.released and owner.held.released and owner.turns.empty():
                    break
                try:
                    turn = await asyncio.wait_for(owner.turns.get(), 0.1)
                except TimeoutError:
                    continue
                if owner.session.error:
                    turn.abort(owner.session.error)
                    raise AudioFailure(owner.session.error)
                succeeded = await self.process(turn, owner, logical=True)
                if not succeeded:
                    owner.session.abort(self.last_error or "held_turn_failed")
                    break
                if not owner.session.released:
                    owner.held.resume()
        except asyncio.CancelledError:
            owner.session.abort("cancelled")
            raise
        except Exception as err:
            self.failures += 1
            self.last_error = str(err) if isinstance(err, AudioFailure) else "held_failed"
            self.outcome("failed", self.last_error)
        finally:
            if owner.release_timer:
                owner.release_timer.cancel()
            microphone.cancel()
            with suppress(asyncio.CancelledError):
                await microphone
            await self.close_microphone(owner)
            self.history.finish(owner.record, error=owner.session.error)
            self.phase = "idle"
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

    async def maintain_microphone(self, session, owner=None):
        owner = owner or self.capture
        next_keepalive = asyncio.get_running_loop().time() + self.keepalive_interval
        while not session.error and owner and not owner.closed:
            if session.released:
                await self.close_microphone(owner)
                return
            await asyncio.sleep(0.05)
            if session.error or session.released or owner.closed:
                continue
            if owner.transport and asyncio.get_running_loop().time() >= next_keepalive:
                try:
                    await owner.transport.write(owner.caps.keepalive_command(owner.stream_id))
                except Exception:
                    session.abort("microphone_keepalive_failed")
                    return
                next_keepalive = asyncio.get_running_loop().time() + self.keepalive_interval
        if session.error and owner:
            await self.close_microphone(owner)
            if owner is self.pending:
                self.expire_pending(owner, session.error)

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
        if self.promotion_task and not self.promotion_task.done():
            self.promotion_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.promotion_task
        for task in tuple(self.capture_tasks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.capture_tasks.clear()
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
        if self.capture and self.capture.expiry:
            self.capture.expiry.cancel()
        self.history.clear()
        self.changed()
