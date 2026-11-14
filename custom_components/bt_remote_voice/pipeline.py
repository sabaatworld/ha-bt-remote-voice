"""Small native-pipeline adapter for externally bounded, release-delimited PCM.

The satellite helper in HA 2026.11 cannot accept AudioSettings. Keep this
compatibility boundary here; never patch Core or change other satellites.
"""

from homeassistant.components import stt
from homeassistant.components.assist_pipeline import async_pipeline_from_audio_stream
from homeassistant.components.assist_pipeline.models import AudioSettings
from homeassistant.core import Context
from homeassistant.helpers import chat_session


async def run_audio_pipeline(satellite, session, start_stage, end_stage, *, fresh=False):
    satellite._run_has_tts = False
    context = Context()
    satellite.async_set_context(context)
    registry = satellite.registry_entry
    with chat_session.async_get_chat_session(
        satellite.hass, None if fresh else satellite._conversation_id, context=None
    ) as chat:
        satellite._conversation_id = chat.conversation_id
        await async_pipeline_from_audio_stream(
            satellite.hass,
            context=context,
            event_callback=satellite._internal_on_pipeline_event,
            stt_metadata=stt.SpeechMetadata(
                language="",
                format=stt.AudioFormats.WAV,
                codec=stt.AudioCodecs.PCM,
                bit_rate=stt.AudioBitRates.BITRATE_16,
                sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                channel=stt.AudioChannels.CHANNEL_MONO,
            ),
            stt_stream=session.stream(),
            pipeline_id=satellite._resolve_pipeline(),
            conversation_id=chat.conversation_id,
            device_id=registry.device_id if registry else None,
            satellite_id=satellite.entity_id,
            tts_audio_output=satellite.tts_options,
            audio_settings=AudioSettings(is_vad_enabled=False),
            start_stage=start_stage,
            end_stage=end_stage,
        )
