"""Bounded utterance input; a normal release is EOF, never pipeline cancellation."""

import asyncio
from collections import deque

from .protocol import AdpcmDecoder, Control, decode_frame


class AudioFailure(Exception):
    """The utterance is incomplete and must not execute an intent."""


class VoiceSession:
    def __init__(
        self,
        frame_size: int,
        *,
        max_bytes=64000,
        first_timeout=3,
        stall_timeout=2,
        max_duration=30,
        finish_delay=0.1,
    ):
        self.frame_size = frame_size
        self.decoder = AdpcmDecoder()
        self.sequence = 0
        self.queue = deque()
        self.queued_bytes = 0
        self.max_bytes = max_bytes
        self.stall_timeout = stall_timeout
        self.finish_delay = finish_delay
        self.error = None
        self.input_complete = False
        self.failed = asyncio.Event()
        self.finished = False
        self.released = False
        self.frames = 0
        self.changed = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.watchdog = self.loop.call_later(first_timeout, self.abort, "no_audio")
        self.limit = self.loop.call_later(max_duration, self.abort, "utterance_too_long")
        self.tail = None

    def audio(self, data: bytes):
        if self.finished:
            return
        try:
            pcm = decode_frame(self.decoder, data, self.frame_size)
        except ValueError:
            self.abort("invalid_audio_frame")
            return
        if self.queued_bytes + len(pcm) > self.max_bytes:
            self.abort("buffer_overflow")
            return
        self.sequence = (self.sequence + 1) & 65535
        self.frames += 1
        self.queue.append(pcm)
        self.queued_bytes += len(pcm)
        self.watchdog.cancel()
        self.watchdog = self.loop.call_later(self.stall_timeout, self.abort, "audio_stalled")
        self.changed.set()

    def sync(self, event: Control):
        if self.finished:
            return
        if event.codec != 2 or event.index > 88 or event.sequence != self.sequence:
            self.abort("audio_sequence_gap")
            return
        self.decoder.reset(event.predictor, event.index)

    def release(self):
        if self.finished or self.released:
            return
        self.released = True
        if self.finish_delay:
            self.tail = self.loop.call_later(self.finish_delay, self.finish)
        else:
            self.finish()

    def finish(self):
        if self.finished:
            return
        if not self.frames:
            self.abort("no_audio")
            return
        self.finished = True
        self._cancel_timers()
        self.changed.set()

    def abort(self, reason: str):
        self.error = reason
        self.failed.set()
        self.finished = True
        self.queue.clear()
        self.queued_bytes = 0
        self._cancel_timers()
        self.changed.set()

    def _cancel_timers(self):
        self.watchdog.cancel()
        self.limit.cancel()
        if self.tail:
            self.tail.cancel()

    async def stream(self):
        while True:
            if self.error:
                raise AudioFailure(self.error)
            if self.queue:
                data = self.queue.popleft()
                self.queued_bytes -= len(data)
                yield data
            elif self.finished:
                self.input_complete = True
                return
            else:
                self.changed.clear()
                await self.changed.wait()
