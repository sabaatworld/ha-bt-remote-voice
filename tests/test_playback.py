import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature

from custom_components.atvv_remote.session import AudioFailure


async def test_reply_waits_for_observed_start_then_finish(hass):
    from custom_components.atvv_remote.playback import play_reply

    hass.states.async_set(
        "media_player.test_speaker",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )

    async def call(*args, **kwargs):
        hass.states.async_set("media_player.test_speaker", "playing")

    with patch.object(type(hass.services), "async_call", side_effect=call) as service:
        task = asyncio.create_task(
            play_reply(
                hass,
                "media_player.test_speaker",
                {"media_id": "media-source://tts/synthetic", "mime_type": "audio/wav"},
                start_timeout=0.1,
                finish_timeout=0.1,
            )
        )
        await asyncio.sleep(0.01)
        assert not task.done()
        hass.states.async_set("media_player.test_speaker", "idle")
        await task
        assert service.call_args.args[2]["entity_id"] == "media_player.test_speaker"


async def test_reply_does_not_claim_completion_for_idle_without_start(hass):
    from custom_components.atvv_remote.playback import play_reply

    hass.states.async_set(
        "media_player.test_speaker",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )
    with patch.object(type(hass.services), "async_call", return_value=None):
        with pytest.raises(AudioFailure, match="playback_start_timeout"):
            await play_reply(
                hass,
                "media_player.test_speaker",
                {"media_id": "media-source://tts/synthetic"},
                start_timeout=0.01,
            )


async def test_busy_or_missing_speaker_is_not_overwritten(hass):
    from custom_components.atvv_remote.playback import play_reply

    for state in ("unavailable", "playing", "buffering"):
        hass.states.async_set(
            "media_player.test_speaker",
            state,
            {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
        )
        with patch.object(type(hass.services), "async_call") as service:
            with pytest.raises(AudioFailure):
                await play_reply(
                    hass, "media_player.test_speaker", {"media_id": "media-source://tts/synthetic"}
                )
            service.assert_not_called()


async def test_cancellation_removes_playback_listener(hass):
    from custom_components.atvv_remote.playback import play_reply

    hass.states.async_set(
        "media_player.test_speaker",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )
    removed = []
    with (
        patch(
            "custom_components.atvv_remote.playback.async_track_state_change_event",
            return_value=lambda: removed.append(True),
        ),
        patch.object(type(hass.services), "async_call", return_value=None),
    ):
        task = asyncio.create_task(
            play_reply(
                hass, "media_player.test_speaker", {"media_id": "media-source://tts/synthetic"}
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert removed == [True]


async def test_playback_finish_timeout_and_unavailability_are_errors(hass):
    from custom_components.atvv_remote.playback import play_reply

    for target_state, expected in (
        (None, "playback_finish_timeout"),
        ("unavailable", "speaker_unavailable"),
    ):
        hass.states.async_set(
            "media_player.test_speaker",
            "idle",
            {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
        )

        async def call(*args, target_state=target_state, **kwargs):
            hass.states.async_set("media_player.test_speaker", "playing")
            if target_state:
                hass.states.async_set("media_player.test_speaker", target_state)

        with patch.object(type(hass.services), "async_call", side_effect=call):
            with pytest.raises(AudioFailure, match=expected):
                await play_reply(
                    hass,
                    "media_player.test_speaker",
                    {"media_id": "media-source://tts/test"},
                    finish_timeout=0.01,
                )
