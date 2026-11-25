"""Bounded utterance input; a normal release is EOF, never pipeline cancellation."""

import asyncio
import sys
from array import array
from collections import deque

from .opus import OpusDecoder
from .protocol import AdpcmDecoder, Capabilities, Control, decode_frame


class AudioFailure(Exception):
    """The utterance is incomplete and must not execute an intent."""


OPUS_ERRORS = ("invalid_opus_packet", "opus_decoder_unavailable")


EARLY_STOP_WINDOW = 1.0  # seconds: a STOP this soon after START may be an early (spurious) one
STOP_QUIET_SECONDS = 0.6  # with early-stop handling: audio must stay quiet this long to end
LATE_AUDIO_FRAMES = 10  # frames after a STOP, and ...
LATE_AUDIO_SECONDS = (
    0.5  # ... audio still arriving this long after it, show the remote kept streaming
)


class VoiceSession:
    def __init__(
        self,
        frame_size: int,
        *,
        caps=None,
        ignore_early_stop=False,
        max_gap_ms=0,
        max_bytes=64000,
        first_timeout=3,
        stall_timeout=2,
        max_duration=30,
        finish_delay=0.1,
        on_pcm=None,
    ):
        self.on_pcm = on_pcm
        self.ignore_early_stop = ignore_early_stop
        self.max_gap_ms = max_gap_ms  # lost audio up to this long is replaced with silence
        self.stop_at = None  # seconds after the start at which the remote's STOP arrived
        self.stop_pending = False  # an early STOP is being held back
        self.frames_after_stop = 0  # frames that arrived after a STOP (the remote kept streaming)
        self.stop_time = self.last_after_stop = (
            None  # loop times of the STOP and of the last frame after it
        )
        self.stop_timer = None
        self.gaps = self.lost_frames = 0  # audio the remote sent but we never received
        self.gaps_covered = 0  # of those gaps, how many were filled with silence
        self.last_pcm_len = 0
        self.last_audio = None
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
        self.limit = self.loop.call_later(max_duration, self.abort, "capture_limit")
        self.tail = None
        self._opus = None

    def _decode_atvv(self, data):
        if self.caps.version == 4:
            if len(data) != self.frame_size or data[5] > 88:
                raise ValueError("invalid_audio_frame")
            seq = int.from_bytes(data[:2], "big")
            if self.legacy_sequence is not None and seq != self.legacy_sequence:
                if not self._cover_gap((seq - self.legacy_sequence) & 65535):
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
        return pcm

    def audio(self, data: bytes):
        if self.stop_at is not None:
            self.frames_after_stop += 1  # counted even when the STOP already ended the utterance
            self.last_after_stop = self.loop.time()
        if self.finished:
            return
        if self.caps.codec == 3:  # Opus (Fire remote): one 80-byte packet per report
            try:
                if len(data) != 80 or data[0] != 0xB8:  # fail closed on anything else
                    raise ValueError("invalid_opus_packet")
                if self._opus is None:
                    self._opus = OpusDecoder()
                pcm = self._opus.decode(data)
            except ValueError as err:
                self.abort(str(err) if str(err) in OPUS_ERRORS else "invalid_opus_packet")
                return
        else:
            try:
                pcm = self._decode_atvv(data)
            except ValueError:
                self.abort("invalid_audio_frame")
                return
            if pcm is None:  # aborted inside the decoder (sequence gap)
                return
        if self.on_pcm is None and self.queued_bytes + len(pcm) > self.max_bytes:
            self.abort("buffer_overflow")
            return
        self.last_pcm_len = len(pcm)
        self.last_audio = self.loop.time()
        self.sequence = (self.sequence + 1) & 65535
        self.frames += 1
        if self.first_audio_ms is None:
            self.first_audio_ms = round((self.loop.time() - self.started_at) * 1000, 1)
        if self.on_pcm is None:
            self.queue.append(pcm)
            self.queued_bytes += len(pcm)
        else:
            self.on_pcm(pcm)
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
            missing = (event.sequence - self.sequence) & 65535
            if not self._cover_gap(missing):
                self.sequence_mismatch = {"expected": event.sequence, "received": self.sequence}
                self.abort("audio_sequence_gap")
                return
            self.sequence = event.sequence
        self.decoder.reset(event.predictor, event.index)

    def _cover_gap(self, missing):
        """Replace ``missing`` lost frames with silence if that is allowed and short enough.

        Always counts the gap (``gaps``, ``lost_frames``) so the test can report it; returns False
        (the caller aborts) when covering is off, the gap is too long, or it is not a loss at all
        (``missing`` of 0, or the remote sent more frames than it announced).
        """
        if not 0 < missing < 32768:  # a huge "gap" is really a frame count that went backwards
            return False
        self.gaps += 1
        self.lost_frames += missing
        frame_ms = self.last_pcm_len / 32 if self.last_pcm_len else 0
        if not self.max_gap_ms or not frame_ms or missing * frame_ms > self.max_gap_ms:
            return False
        silence = bytes(self.last_pcm_len) * missing
        if self.on_pcm is None:
            if self.queued_bytes + len(silence) > self.max_bytes:
                return False
            self.queue.append(silence)
            self.queued_bytes += len(silence)
        else:
            self.on_pcm(silence)
        if self.vad:
            self.vad_buffer.extend(silence)
        self.gaps_covered += 1
        return True

    @property
    def early_stop_seen(self):
        """The remote sent STOP soon after START and then kept streaming (it was still held)."""
        return (
            self.stop_at is not None
            and self.stop_at < EARLY_STOP_WINDOW
            and self.frames_after_stop >= LATE_AUDIO_FRAMES
            # A quick release can have a short burst of buffered audio behind it; a still-held
            # button keeps streaming for much longer than that.
            and self.last_after_stop - self.stop_time >= LATE_AUDIO_SECONDS
        )

    def stop_would_be_held_back(self):
        """True when a STOP arriving now would be held back (so the capture stays open)."""
        return (
            self.ignore_early_stop
            and not self.finished
            and not self.released
            and not self.stop_pending
            and self.loop.time() - self.started_at < EARLY_STOP_WINDOW
        )

    def remote_stop(self):
        """The remote's STOP. With early-stop handling a STOP in the first second is held back
        while audio keeps arriving; a second STOP, or quiet audio, ends the utterance."""
        if self.finished or self.released:
            return
        if self.stop_pending:  # the second STOP: the remote really is done
            self.release()
            return
        elapsed = self.loop.time() - self.started_at
        if self.stop_at is None:
            self.stop_at = elapsed
            self.stop_time = self.loop.time()
        if self.ignore_early_stop and elapsed < EARLY_STOP_WINDOW:
            self.stop_pending = True
            self.stop_timer = self.loop.call_later(STOP_QUIET_SECONDS, self._release_when_quiet)
            return
        self.release()

    def _release_when_quiet(self):
        if self.finished or self.released:
            return
        last = self.last_audio if self.last_audio is not None else self.started_at
        quiet = self.loop.time() - last
        if quiet >= STOP_QUIET_SECONDS:
            self.release()
        else:
            self.stop_timer = self.loop.call_later(
                STOP_QUIET_SECONDS - quiet, self._release_when_quiet
            )

    def release(self):
        if self.finished or self.released:
            return
        self.released = True
        self.released_at = self.loop.time()
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
        if self.stop_timer:
            self.stop_timer.cancel()

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
