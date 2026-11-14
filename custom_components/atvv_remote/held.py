"""Logical PCM requests independent of a physical capture's codec and sequence."""

import asyncio
from collections import deque

from .preferences import capture_buffer_bytes
from .session import AudioFailure


class Utterance:
    def __init__(self, *, max_bytes=960000, max_seconds=30.0, on_limit=None):
        self.queue = deque()
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.on_limit = on_limit
        self.limit = None
        self.queued_bytes = 0
        self.frames = 0
        self.finished = self.released = self.input_complete = False
        self.error = None
        self.failed = asyncio.Event()
        self.changed = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.started_at = self.loop.time()
        self.first_audio_ms = None
        self.sequence_mismatch = None

    def feed(self, pcm):
        if self.finished:
            return
        if self.queued_bytes + len(pcm) > self.max_bytes:
            self.abort("buffer_overflow")
            return
        self.queue.append(pcm)
        self.queued_bytes += len(pcm)
        self.frames += 1
        if self.limit is None:
            self.limit = self.loop.call_later(self.max_seconds, self._limit_reached)
        if self.first_audio_ms is None:
            self.first_audio_ms = round((self.loop.time() - self.started_at) * 1000, 1)
        self.changed.set()

    def _limit_reached(self):
        if self.finished:
            return
        self.abort("capture_limit")
        if self.on_limit:
            self.on_limit(self)

    def _cancel_limit(self):
        if self.limit is not None:
            self.limit.cancel()

    def release(self):
        if not self.finished:
            self._cancel_limit()
            self.released = self.finished = True
            self.released_at = self.loop.time()
            self.changed.set()

    def abort(self, reason):
        self._cancel_limit()
        self.error = reason
        self.finished = True
        self.queue.clear()
        self.queued_bytes = 0
        self.failed.set()
        self.changed.set()

    async def stream(self):
        while True:
            if self.error:
                raise AudioFailure(self.error)
            if self.queue:
                chunk = self.queue.popleft()
                self.queued_bytes -= len(chunk)
                yield chunk
            elif self.finished:
                self.input_complete = True
                return
            else:
                self.changed.clear()
                await self.changed.wait()


class HeldTurns:
    def __init__(self, on_turn, *, max_request_seconds=30):
        from pymicro_vad import MicroVad

        self.vad = MicroVad()
        self.on_turn = on_turn
        self.max_request_seconds = max_request_seconds
        self.current = None
        self.buffer = bytearray()
        self.blocked = self.released = False
        self.speech = self.silence = 0

    def feed(self, pcm, *, probability=None):
        if self.blocked or self.released:
            return
        self.buffer.extend(pcm)
        while len(self.buffer) >= 320 and not self.blocked:
            chunk = bytes(self.buffer[:320])
            del self.buffer[:320]
            p = probability if probability is not None else self.vad.Process10ms(chunk)
            if p >= 0.5:
                if self.current is None:
                    self.current = Utterance(
                        max_bytes=capture_buffer_bytes(self.max_request_seconds),
                        max_seconds=self.max_request_seconds,
                        on_limit=self._turn_limit,
                    )
                self.speech += 1
                self.silence = 0
            elif self.current is not None:
                self.silence += 1
            if self.current is not None:
                self.current.feed(chunk)
                if self.current.error:
                    self._complete()
                elif self.speech >= 30 and self.silence >= 80:
                    self._complete()
                elif self.speech < 30 and self.silence >= 80:
                    self.current.abort("no_speech")
                    self.current = None
                    self.speech = self.silence = 0

    def _turn_limit(self, turn):
        # The turn hit its duration limit while the person is still talking: deliver
        # the failed turn now so the pipeline reports capture_limit.
        if turn is self.current:
            self._complete()

    def _complete(self):
        turn, self.current = self.current, None
        turn.release()
        self.blocked = True
        self.buffer.clear()
        self.on_turn(turn)

    def resume(self):
        self.blocked = False
        self.speech = self.silence = 0
        self.buffer.clear()

    def close(self):
        """Drop the turn in progress and its timer (stop, unload, disconnect, failed hold)."""
        self.released = True
        if self.current is not None:
            self.current.abort("cancelled")
            self.current = None

    def release(self):
        self.released = True
        if self.current is not None:
            if self.buffer:
                self.current.feed(bytes(self.buffer))
                self.buffer.clear()
            if self.speech >= 30:
                self._complete()
            else:
                self.current.abort("no_speech")
                self.current = None
