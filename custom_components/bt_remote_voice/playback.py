"""Observe real media-player playback; a generated TTS URL is not completion."""

import asyncio

from homeassistant.components.media_player import (
    MediaPlayerEntityFeature,
    async_process_play_media_url,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .session import AudioFailure


def speaker_choices(hass):
    """Media players that can play audio (names are user data, not translated)."""
    return {
        s.entity_id: s.name
        for s in hass.states.async_all("media_player")
        if s.attributes.get("supported_features", 0) & MediaPlayerEntityFeature.PLAY_MEDIA
    }


# Speaker problems before any sound was started: the action ran (or runs) and only the spoken reply
# is lost. Start/stop/interrupt/finish failures and a speaker lost *after* playback began
# (``AudioFailure.after_start``) are deliberately not listed or are excluded: the media may still be
# playing or start late, so those stay failures.
SKIPPABLE = frozenset({"speaker_unavailable", "speaker_busy", "speaker_cannot_play"})
# Speaker codes that count towards the "speaker is not working" Repairs hint. A busy speaker is
# ordinary (music, an announcement) and neither counts nor resets it.
COUNTED = SKIPPABLE - {"speaker_busy"}


def speaker_blocker(hass, speaker):
    """``speaker_unavailable`` when the reply speaker cannot be used right now, else None.

    Checked once at the start of a request so no reply is generated for a speaker that is not there.
    A missing ``hass.states`` (bare test objects) never blocks.
    """
    states = getattr(hass, "states", None)
    if not speaker or states is None:
        return None
    state = states.get(speaker)
    if state is None or state.state in ("unknown", "unavailable"):
        return "speaker_unavailable"
    return None


async def play_reply(
    hass, speaker, output, *, start_timeout=10, finish_timeout=90, stop_timeout=3, on_stage=None
):
    state = hass.states.get(speaker)
    if state is None or state.state in ("unknown", "unavailable"):
        raise AudioFailure("speaker_unavailable")
    if state.state in ("playing", "buffering", "paused"):
        raise AudioFailure("speaker_busy")
    if not state.attributes.get("supported_features", 0) & MediaPlayerEntityFeature.PLAY_MEDIA:
        raise AudioFailure("speaker_cannot_play")
    media_id = output.get("media_id")
    if not media_id and output.get("url"):
        media_id = async_process_play_media_url(hass, output["url"])
    if not media_id:
        raise AudioFailure("missing_tts_audio")
    loop = asyncio.get_running_loop()
    started, finished = loop.create_future(), loop.create_future()

    def failure(code):
        err = AudioFailure(code)
        err.after_start = started.done()  # a speaker lost mid-playback is not a skipped reply
        return err

    @callback
    def changed(event):
        new = event.data.get("new_state")
        if new is None or new.state in ("unknown", "unavailable"):
            if not finished.done():
                finished.set_result("speaker_unavailable")
        elif new.state == "playing":
            if not started.done():
                started.set_result(None)
                if on_stage:
                    on_stage("reported_playing")
        elif started.done() and not finished.done():
            if new.state in ("idle", "off", "on"):
                finished.set_result(None)
                if on_stage:
                    on_stage("playback_complete")
            elif new.state == "paused":
                finished.set_result("playback_interrupted")

    remove = async_track_state_change_event(hass, [speaker], changed)
    try:
        if on_stage:
            on_stage("play_requested")
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
            raise failure(finished.result())
        try:
            error = await asyncio.wait_for(finished, finish_timeout)
        except TimeoutError as err:
            raise AudioFailure("playback_finish_timeout") from err
        if error:
            raise failure(error)
    except asyncio.CancelledError, Exception:
        # Stop only this request's media on *every* abnormal exit, including
        # service/finish timeouts. A different source may have taken the player.
        current = hass.states.get(speaker)
        if current and current.state in ("playing", "buffering", "paused"):
            actual = current.attributes.get("media_content_id")
            if actual and actual in (media_id, output.get("url")):
                try:
                    await asyncio.wait_for(
                        hass.services.async_call(
                            "media_player", "media_stop", {"entity_id": speaker}, blocking=True
                        ),
                        stop_timeout,
                    )
                    async with asyncio.timeout(stop_timeout):
                        while True:
                            stopped = hass.states.get(speaker)
                            if stopped and stopped.state in ("idle", "off", "on"):
                                break
                            if stopped and stopped.attributes.get("media_content_id") != actual:
                                raise AudioFailure("playback_stop_unverified")
                            await asyncio.sleep(0.02)
                except AudioFailure:
                    raise
                except Exception:
                    raise AudioFailure("playback_stop_failed") from None
            else:
                raise AudioFailure("playback_stop_unverified") from None
        raise
    finally:
        remove()
        for future in (started, finished):
            if not future.done():
                future.cancel()
