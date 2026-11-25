"""Test clips: names, WAV, media folder, signed links."""

import wave
from datetime import datetime
from io import BytesIO

from custom_components.bt_remote_voice import clips
from custom_components.bt_remote_voice.clips import (
    FOLDER,
    Clip,
    clip_links,
    clip_name,
    media_target,
    save_clips,
    wav_bytes,
)

MOMENT = datetime(2026, 11, 25, 14, 30, 7)


def clip(pcm=b"\x01\x00" * 1600, started=MOMENT):
    return Clip(started=started, pcm=pcm)


def test_clip_name_has_seconds():
    assert clip_name(MOMENT) == "Test clip 2026-11-25 14-30-07.wav"
    assert clip_name(MOMENT, 2) == "Test clip 2026-11-25 14-30-07-2.wav"


def test_wav_bytes_header():
    pcm = b"\x01\x00" * 1600
    with wave.open(BytesIO(wav_bytes(pcm))) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)
        assert wav.getnframes() == 1600
        assert wav.readframes(1600) == pcm


async def test_media_target_prefers_local(hass, tmp_path):
    hass.config.media_dirs = {"other": str(tmp_path / "x"), "local": str(tmp_path)}
    assert await media_target(hass) == ("local", tmp_path)


async def test_media_target_first_when_no_local(hass, tmp_path):
    hass.config.media_dirs = {"recordings": str(tmp_path)}
    assert await media_target(hass) == ("recordings", tmp_path)


async def test_media_target_none_without_dirs_or_directory(hass, tmp_path):
    hass.config.media_dirs = {}
    assert await media_target(hass) is None
    hass.config.media_dirs = {"local": str(tmp_path / "missing")}
    assert await media_target(hass) is None
    (tmp_path / "file").write_text("x")
    hass.config.media_dirs = {"local": str(tmp_path / "file")}
    assert await media_target(hass) is None


async def test_save_creates_subfolder_and_files(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    saved = await save_clips(hass, [clip(), clip(started=datetime(2026, 11, 25, 14, 30, 9))])
    assert saved.error is None and saved.source == "local"
    assert saved.names == ["Test clip 2026-11-25 14-30-07.wav", "Test clip 2026-11-25 14-30-09.wav"]
    assert saved.seconds == [0.1, 0.1]
    for name in saved.names:
        assert (tmp_path / FOLDER / name).read_bytes()[:4] == b"RIFF"


async def test_same_second_clips_get_suffixes_and_never_overwrite(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    folder = tmp_path / FOLDER
    folder.mkdir()
    (folder / clip_name(MOMENT)).write_bytes(b"old")
    saved = await save_clips(hass, [clip(), clip()])
    assert saved.names == [clip_name(MOMENT, 2), clip_name(MOMENT, 3)]
    assert (folder / clip_name(MOMENT)).read_bytes() == b"old"


async def test_empty_pcm_skipped_and_cap_ten(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    saved = await save_clips(hass, [clip(pcm=b"")] + [clip() for _ in range(12)])
    assert len(saved.names) == clips.MAX_CLIPS


async def test_no_media_folder_reports_it(hass):
    hass.config.media_dirs = {}
    saved = await save_clips(hass, [clip()])
    assert saved.error == "no_media_folder" and saved.names == []


async def test_unwritable_reports_write_failed_without_raising(hass, tmp_path):
    hass.config.media_dirs = {"local": str(tmp_path)}
    tmp_path.chmod(0o500)
    try:
        saved = await save_clips(hass, [clip()])
    finally:
        tmp_path.chmod(0o700)
    assert saved.error == "write_failed" and saved.names == []


async def test_clip_links_use_real_signing_and_percent_encode(hass, tmp_path):
    from homeassistant.components.http.auth import STORAGE_KEY

    hass.data[STORAGE_KEY] = "content-user-token-id"
    hass.config.media_dirs = {"local": str(tmp_path)}
    saved = await save_clips(hass, [clip()])
    text = clip_links(hass, saved)
    href = text.split('href="')[1].split('"')[0]
    assert href.startswith(
        "/media/local/Bluetooth%20Remote%20Voice/Test%20clip%202026-11-25%2014-30-07.wav?authSig="
    )
    assert " " not in href  # percent-encoded after signing
    assert 'target="_blank"' in text  # opens in a new tab, not over the dialog
    assert "14:30:07" in text and "0.1 s" in text
