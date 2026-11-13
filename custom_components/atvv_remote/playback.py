"""Observe real media-player playback; a generated TTS URL is not completion."""

import asyncio

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.helpers.event import async_track_state_change_event

from .session import AudioFailure


def speaker_choices(hass):
    return {
        "": "No spoken reply",
        **{
            s.entity_id: s.name
            for s in hass.states.async_all("media_player")
            if s.attributes.get("supported_features", 0) & MediaPlayerEntityFeature.PLAY_MEDIA
        },
    }


async def play_reply(hass, speaker, output, *, start_timeout=10, finish_timeout=90):
    state = hass.states.get(speaker)
    if state is None or state.state in ("unknown", "unavailable"):
        raise AudioFailure("speaker_unavailable")
    if state.state in ("playing", "buffering", "paused"):
        raise AudioFailure("speaker_busy")
    if not state.attributes.get("supported_features", 0) & MediaPlayerEntityFeature.PLAY_MEDIA:
        raise AudioFailure("speaker_cannot_play")
    media_id = output.get("media_id")
    if not media_id:
        raise AudioFailure("missing_tts_audio")
    loop = asyncio.get_running_loop()
    started, finished = loop.create_future(), loop.create_future()

    def changed(event):
        new = event.data.get("new_state")
        if new is None or new.state in ("unknown", "unavailable"):
            if not finished.done():
                finished.set_result("speaker_unavailable")
        elif new.state == "playing":
            if not started.done():
                started.set_result(None)
        elif started.done() and not finished.done():
            if new.state in ("idle", "off", "on"):
                finished.set_result(None)
            elif new.state == "paused":
                finished.set_result("playback_interrupted")

    remove = async_track_state_change_event(hass, [speaker], changed)
    try:
        await asyncio.wait_for(
            hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": speaker,
                    "media_content_id": media_id,
                    "media_content_type": output.get("mime_type", "music"),
                    "announce": bool(
                        state.attributes.get("supported_features", 0)
                        & MediaPlayerEntityFeature.MEDIA_ANNOUNCE
                    ),
                },
                blocking=True,
            ),
            15,
        )
        done, _ = await asyncio.wait(
            (started, finished), timeout=start_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise AudioFailure("playback_start_timeout")
        if finished.done() and finished.result():
            raise AudioFailure(finished.result())
        try:
            error = await asyncio.wait_for(finished, finish_timeout)
        except TimeoutError as err:
            raise AudioFailure("playback_finish_timeout") from err
        if error:
            raise AudioFailure(error)
    except TimeoutError as err:
        raise AudioFailure("playback_service_timeout") from err
    finally:
        remove()
        for future in (started, finished):
            if not future.done():
                future.cancel()
