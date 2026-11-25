"""Test clips: where they are saved, how they are named, and how a user reaches them.

A clip is the audio of one voice press during "Test my remote", saved as a WAV in Home Assistant's
media folder (login-only, listed under Media, My media). Nothing here talks to Bluetooth.
"""

import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from homeassistant.components.http.auth import async_sign_path

FOLDER = "Bluetooth Remote Voice"
MAX_CLIPS = 10
LINK_MINUTES = 5
SAMPLE_RATE = 16000
SAMPLE_BYTES = 2  # 16-bit mono


@dataclass
class Clip:
    started: datetime
    pcm: bytes
    frames: int = 0  # audio frames received for this press
    ended: str = ""  # how the press ended: released, speech_end, cut or the session error code
    reason: int | None = None  # the remote's start reason for this press (3 = held)


@dataclass
class SavedClips:
    source: str = ""
    names: list = field(default_factory=list)
    seconds: list = field(default_factory=list)
    times: list = field(default_factory=list)
    error: str | None = None  # "no_media_folder" or "write_failed"


def clip_name(moment, suffix=1):
    base = f"Test clip {moment:%Y-%m-%d %H-%M-%S}"
    return f"{base}.wav" if suffix == 1 else f"{base}-{suffix}.wav"


def wav_bytes(pcm):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_BYTES)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _target(media_dirs):
    """(source id, folder) of the media folder to use, or None."""
    if not media_dirs:
        return None
    source = "local" if "local" in media_dirs else next(iter(media_dirs))
    path = Path(media_dirs[source])
    return (source, path) if path.is_dir() else None


async def media_target(hass):
    return await hass.async_add_executor_job(_target, dict(hass.config.media_dirs))


def _write(folder, clips):
    """Blocking part of ``save_clips``; returns (names, seconds, times)."""
    folder.mkdir(exist_ok=True)  # the media folder itself must already exist
    names, seconds, times = [], [], []
    for item in clips:
        if not item.pcm or len(names) >= MAX_CLIPS:
            continue
        data, suffix = wav_bytes(item.pcm), 1
        while True:
            name = clip_name(item.started, suffix)
            try:
                with open(folder / name, "xb") as handle:
                    handle.write(data)
            except FileExistsError:
                suffix += 1
                continue
            break
        names.append(name)
        seconds.append(round(len(item.pcm) / (SAMPLE_RATE * SAMPLE_BYTES), 1))
        times.append(item.started)
    return names, seconds, times


async def save_clips(hass, clips):
    """Save the clips; never raises (a clip that cannot be saved must not fail the test)."""
    target = await media_target(hass)
    if target is None:
        return SavedClips(error="no_media_folder")
    source, root = target
    saved = SavedClips(source=source)
    try:
        saved.names, saved.seconds, saved.times = await hass.async_add_executor_job(
            _write, root / FOLDER, clips
        )
    except OSError:
        saved.error = "write_failed"
    return saved


def clip_links(hass, saved):
    """One markdown line per clip with a short-lived signed link to the existing media view."""
    lines = []
    for name, seconds, moment in zip(saved.names, saved.seconds, saved.times, strict=True):
        path = f"/media/{saved.source}/{FOLDER}/{name}"
        # async_sign_path returns a decoded path: quote it afterwards, never before.
        signed = async_sign_path(hass, path, timedelta(minutes=LINK_MINUTES))
        route, _, query = signed.partition("?")
        link = f"{quote(route)}?{query}" if query else quote(route)
        # Raw HTML, not Markdown: only an explicit target opens the clip in a new tab instead of
        # leaving the dialog (the frontend keeps same-site Markdown links in the same tab).
        lines.append(
            f'- <a href="{escape(link)}" target="_blank" rel="noopener">{moment:%H:%M:%S} · {seconds} s</a>'
        )
    return "\n".join(lines)
