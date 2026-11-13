"""Bounded utterance input; a normal release is EOF, never pipeline cancellation."""

import asyncio
import sys
from array import array
from collections import deque

from .protocol import AdpcmDecoder, Capabilities, Control, decode_frame


class AudioFailure(Exception):
    """The utterance is incomplete and must not execute an intent."""


class VoiceSession:
    def __init__(
        self,
        frame_size: int,
        *,
        caps=None,
        max_bytes=64000,
        first_timeout=3,
        stall_timeout=2,
        max_duration=30,
        finish_delay=0.1,
    ):
        self.caps = caps or Capabilities(frame_size)
        self.frame_size = frame_size
        self.sequence_mismatch = None
        self.legacy_sequence = None
        self.previous_sample = None
        self.started_at = asyncio.get_running_loop().time()
        self.first_audio_ms = None
        self.vad = None
        self.segmenter = None
        self.vad_buffer = bytearray()
        if self.caps.model != 3:
            from homeassistant.components.assist_pipeline.vad import VoiceCommandSegmenter
            from pymicro_vad import MicroVad

            self.vad = MicroVad()
            self.segmenter = VoiceCommandSegmenter(timeout_seconds=15, silence_seconds=0.8)
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
            if self.caps.version == 4:
                if len(data) != self.frame_size or data[5] > 88:
                    raise ValueError("invalid_audio_frame")
                seq = int.from_bytes(data[:2], "big")
                if self.legacy_sequence is not None and seq != self.legacy_sequence:
                    self.abort("audio_sequence_gap")
                    return
                self.legacy_sequence = (seq + 1) & 65535
                predictor = int.from_bytes(data[3:5], "big", signed=True)
                self.decoder.reset(predictor, data[5])
                samples = array("h", [predictor] + self.decoder.decode(data[6:]))
                if sys.byteorder != "little":
                    samples.byteswap()
                pcm = samples.tobytes()
            else:
                pcm = decode_frame(self.decoder, data, self.frame_size)
            if self.caps.sample_rate == 8000:
                samples = array("h")
                samples.frombytes(pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                output = array("h")
                for sample in samples:
                    previous = sample if self.previous_sample is None else self.previous_sample
                    output.extend(((previous + sample) // 2, sample))
                    self.previous_sample = sample
                if sys.byteorder != "little":
                    output.byteswap()
                pcm = output.tobytes()
        except ValueError:
            self.abort("invalid_audio_frame")
            return
        if self.queued_bytes + len(pcm) > self.max_bytes:
            self.abort("buffer_overflow")
            return
        self.sequence = (self.sequence + 1) & 65535
        self.frames += 1
        if self.first_audio_ms is None:
            self.first_audio_ms = round((self.loop.time() - self.started_at) * 1000, 1)
        self.queue.append(pcm)
        self.queued_bytes += len(pcm)
        self.watchdog.cancel()
        self.watchdog = self.loop.call_later(self.stall_timeout, self.abort, "audio_stalled")
        self.changed.set()
        if self.vad:
            self.vad_buffer.extend(pcm)
            while len(self.vad_buffer) >= 320:
                chunk = bytes(self.vad_buffer[:320])
                del self.vad_buffer[:320]
                if not self.segmenter.process(0.01, self.vad.Process10ms(chunk)):
                    if self.segmenter.timed_out:
                        self.abort("no_speech")
                    else:
                        self.released = True
                        self.finish()
                    break

    def sync(self, event: Control):
        if self.finished:
            return
        if self.caps.version == 4:
            return  # Per-frame sequence headers validate this format directly.
        if event.codec != self.caps.codec or event.index > 88:
            self.abort("invalid_audio_sync")
            return
        if event.sequence != self.sequence:
            self.sequence_mismatch = {"expected": event.sequence, "received": self.sequence}
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
