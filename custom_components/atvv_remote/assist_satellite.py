"""ATVV microphone satellite using HA's native pipeline lifecycle."""

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
from .playback import play_reply
from .session import AudioFailure


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RemoteSatellite(entry.runtime_data)])


class RemoteSatellite(RemoteEntity, AssistSatelliteEntity):
    _attr_name = "Voice"
    _attr_icon = "mdi:microphone-message"

    def __init__(self, coordinator):
        super().__init__(coordinator, "voice")
        self.current_session = None
        self.tts_output = None
        coordinator.run_pipeline = self.run

    @property
    def extra_state_attributes(self):
        c = self.coordinator
        return {
            "ready": c.ready,
            "last_error": c.last_error,
            "mode": c.entry.options.get("mode", c.entry.data.get("mode", "transcription")),
            "sessions": c.sessions,
            "failures": c.failures,
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

    def _resolve_vad_sensitivity(self):
        # Physical release defines EOF. Keep HA's silence detector from ending a
        # valid <=30 s hold early. EOF still reaches STT immediately on release.
        return 31.0

    async def run(self, session):
        self._resolve_pipeline()
        self.current_session = session
        mode = self.coordinator.entry.options.get(
            "mode", self.coordinator.entry.data.get("mode", "transcription")
        )
        speaker = self.coordinator.entry.options.get("speaker", "")
        self.tts_output = None
        self.pipeline_error = None
        try:
            await self.async_accept_pipeline_from_satellite(
                session.stream(),
                start_stage=PipelineStage.STT,
                end_stage=(PipelineStage.TTS if speaker else PipelineStage.INTENT)
                if mode == "commands"
                else PipelineStage.STT,
            )
            if self.pipeline_error:
                raise AudioFailure(self.pipeline_error)
            if mode == "commands" and speaker:
                if self.tts_output:
                    await play_reply(self.hass, speaker, self.tts_output)
                    self.tts_response_finished()
                elif self.coordinator.last_error:
                    raise AudioFailure("tts_pipeline_failed")
        finally:
            # Failure/cancellation resets state without claiming playback finished.
            if self.state == AssistSatelliteState.RESPONDING:
                self._set_state(AssistSatelliteState.IDLE)
            self.current_session = None
            self.tts_output = None

    def on_pipeline_event(self, event):
        # A provider can stop early independently of host VAD. Do not execute
        # any intent if the user has not released or input was corrupted.
        if event.type == PipelineEventType.STT_END:
            s = self.current_session
            if s is None or not s.finished or not s.input_complete or s.error:
                raise AudioFailure("incomplete_utterance")
        elif event.type == PipelineEventType.TTS_END:
            self.tts_output = (event.data or {}).get("tts_output")
        elif event.type == PipelineEventType.ERROR:
            self.pipeline_error = (event.data or {}).get("code", "pipeline_failed")
            self.coordinator.last_error = self.pipeline_error
        self.coordinator.changed()
