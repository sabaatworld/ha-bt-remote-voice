"""Bluetooth remote microphone satellite using HA's native pipeline lifecycle."""

import asyncio
from contextlib import suppress

from homeassistant.components.assist_pipeline import (
    PipelineEventType,
    PipelineStage,
    async_get_pipelines,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
)
from homeassistant.components.assist_satellite.entity import AssistSatelliteState

from .entity import RemoteEntity
from .pipeline import run_audio_pipeline
from .playback import play_reply
from .preferences import error_message
from .session import AudioFailure


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RemoteSatellite(entry.runtime_data)])


class RemoteSatellite(RemoteEntity, AssistSatelliteEntity):
    _attr_translation_key = "voice"
    generation_wait = 5  # seconds to wait for a cancelled generation before blocking recovery
    _attr_icon = "mdi:microphone-message"

    def __init__(self, coordinator):
        super().__init__(coordinator, "voice")
        self.current_session = None
        self.tts_output = None
        self.reply_allowed = True
        coordinator.run_pipeline = self.run
        coordinator.satellite = self

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        return {
            "ready": c.ready,
            "last_error": c.last_error,
            "last_error_message": error_message(c.hass, c.last_error),
            "mode": c.entry.options.get("mode", c.entry.data.get("mode", "transcription")),
            "sessions": c.sessions,
            "failures": c.failures,
            "rejected": c.rejected,
            "queued": c.queued,
            "last_outcome": c.last_outcome,
            "full_duplex": "unavailable",
        }

    def async_get_configuration(self):
        return AssistSatelliteConfiguration([], [], 0)

    async def async_set_configuration(self, config):
        if config.active_wake_words:
            raise ValueError("wake_words_not_supported")

    def _resolve_pipeline(self):
        # HA's default resolver uses display names and silently falls back.
        # Retain the explicitly selected stable ID and reject missing pipelines.
        entry = self.coordinator.entry
        selected = entry.options.get("pipeline_id", entry.data["pipeline_id"])
        if selected not in {p.id for p in async_get_pipelines(self.hass)}:
            raise AudioFailure("pipeline_removed")
        return selected

    async def async_accept_pipeline_from_satellite(self, audio_stream, start_stage, end_stage):
        # audio_stream is session.stream(); keep this method patchable for HA tests.
        await run_audio_pipeline(
            self,
            self.current_session,
            start_stage,
            end_stage,
            fresh=self.coordinator.entry.options.get("context_mode", "reuse") == "fresh",
        )

    def timing(self, stage, session=None):
        session = session or self.current_session
        if session is not self.current_session or session is None:
            return
        self.coordinator.last_timings[stage + "_ms"] = round(
            (session.loop.time() - session.started_at) * 1000, 1
        )
        self.coordinator.changed()

    async def _start_after(self, output, seconds):
        # Gives a live provider time to build a lead before the speaker starts
        # pulling the stream. Generation completing sooner starts playback anyway.
        await asyncio.sleep(seconds)
        self.start_playback(output)

    def start_playback(self, output):
        # Only the run that owns the pipeline may play; a generation that outlived
        # its run (current_session is None) must never start untracked playback.
        # A reply the user's condition said not to speak never plays.
        if (
            self.current_session is None
            or self.playback_task is not None
            or not self.speaker
            or not self.reply_allowed
        ):
            return
        self.coordinator.phase = "playback"
        self._run_has_tts = True
        self.playback_task = asyncio.create_task(
            play_reply(
                self.hass,
                self.speaker,
                output,
                on_stage=lambda stage, session=self.current_session: self.timing(stage, session),
            ),
            name="BT voice reply",
        )
        self.playback_ready.set()
        if self.entity_id:
            self._set_state(AssistSatelliteState.RESPONDING)
        self.coordinator.changed()

    async def run(self, session):
        self._resolve_pipeline()
        self.current_session = session
        opts = self.coordinator.entry.options
        mode = opts.get("mode", self.coordinator.entry.data.get("mode", "transcription"))
        self.speaker = opts.get("speaker", "") if mode == "commands" else ""
        self.tts_output = self.streaming_output = None
        self.playback_task = None
        self.head_start_task = None
        self.reply_allowed = True
        if self.speaker:
            # Decided once per request (each held turn, and a queued request when it
            # starts); the pipeline below runs unchanged either way.
            entry = self.coordinator.entry
            allowed, code = self.coordinator.reply_condition.decide(
                {
                    "speaker": self.speaker,
                    "remote": getattr(entry, "title", ""),
                    "held": opts.get("input_mode") == "conversation",
                }
            )
            self.reply_allowed = allowed
            if not allowed:
                self.coordinator.outcome("reply_skipped", code)
        self.pipeline_error = None
        self.playback_ready = asyncio.Event()
        self.generation_complete = False
        self.coordinator.phase = "processing"
        self.coordinator.last_timings = {}
        generation = asyncio.create_task(
            self.async_accept_pipeline_from_satellite(
                session.stream(),
                start_stage=PipelineStage.STT,
                end_stage=(PipelineStage.TTS if self.speaker else PipelineStage.INTENT)
                if mode == "commands"
                else PipelineStage.STT,
            ),
            name="BT voice generation",
        )
        readiness = asyncio.create_task(self.playback_ready.wait())
        try:
            while not generation.done():
                watch = {generation}
                if self.playback_task is None:
                    watch.add(readiness)
                elif not self.playback_task.done():
                    watch.add(self.playback_task)
                else:
                    self.playback_task.result()
                await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
                if self.playback_task and self.playback_task.done():
                    self.playback_task.result()
            await generation
            self.generation_complete = True
            self.timing("generation_complete")
            if self.pipeline_error:
                raise AudioFailure(self.pipeline_error)
            if self.speaker:
                if self.playback_task is None and self.tts_output:
                    self.start_playback(self.tts_output)
                if self.playback_task:
                    await asyncio.shield(self.playback_task)
                    self.tts_response_finished()
        finally:
            stop_error = None
            if self.head_start_task:
                self.head_start_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.head_start_task
            readiness.cancel()
            with suppress(asyncio.CancelledError):
                await readiness
            if not generation.done():
                generation.cancel()
                done, _ = await asyncio.wait({generation}, timeout=self.generation_wait)
                if not done:
                    self.coordinator.retiring_generation = generation
                    self.coordinator.recovery_blocked = True
                    generation.add_done_callback(self.retired_generation)
                    stop_error = AudioFailure("generation_retirement_failed")
                else:
                    with suppress(asyncio.CancelledError, Exception):
                        generation.result()
            if self.playback_task and not self.playback_task.done():
                self.playback_task.cancel()
                try:
                    await self.playback_task
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    stop_error = err
            elif self.playback_task:
                # Retrieve failures even if generation failed before awaiting playback.
                with suppress(asyncio.CancelledError, Exception):
                    self.playback_task.result()
            if self.state != AssistSatelliteState.IDLE:
                self._set_state(AssistSatelliteState.IDLE)
            self.current_session = None
            self.tts_output = self.streaming_output = None
            if stop_error:
                raise stop_error

    @staticmethod
    def retired_generation(task):
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    def on_pipeline_event(self, event):
        if self.current_session is None:
            return  # late event from a generation that outlived its run
        # A provider can stop early independently of host VAD. Do not execute
        # the normal HA intent handoff before validated EOF. Native-live providers
        # may already have executed tools during STT; never promise rollback.
        if event.type == PipelineEventType.STT_END:
            s = self.current_session
            if s and hasattr(s, "released_at"):
                self.coordinator.last_timings["input_released_ms"] = round(
                    (s.released_at - s.started_at) * 1000, 1
                )
            self.timing("stt_end")
            if s is None or not s.finished or not s.input_complete or s.error:
                raise AudioFailure("incomplete_utterance")
        elif event.type == PipelineEventType.RUN_START:
            self.streaming_output = (event.data or {}).get("tts_output")
        elif event.type == PipelineEventType.INTENT_PROGRESS:
            output = self.streaming_output or {}
            if (
                (event.data or {}).get("tts_start_streaming")
                and output.get("stream_response")
                and self.coordinator.entry.options.get("output_mode", "auto") == "auto"
                and self.reply_allowed
            ):
                self.timing("output_ready")
                head_start = self.coordinator.entry.options.get("stream_head_start", 0)
                if head_start > 0:
                    self.coordinator.last_timings["head_start_ms"] = round(head_start * 1000)
                    self.head_start_task = asyncio.create_task(
                        self._start_after(output, head_start), name="BT voice head start"
                    )
                else:
                    self.start_playback(output)
        elif event.type == PipelineEventType.TTS_START:
            if not self.reply_allowed and self.entity_id:
                # HA's base handler just set RESPONDING; nothing will be spoken.
                self._set_state(AssistSatelliteState.PROCESSING)
        elif event.type == PipelineEventType.TTS_END:
            self.tts_output = (event.data or {}).get("tts_output")
            self.timing("tts_end")
        elif event.type == PipelineEventType.ERROR:
            self.pipeline_error = (event.data or {}).get("code", "pipeline_failed")
            self.coordinator.last_error = self.pipeline_error
        self.coordinator.changed()
