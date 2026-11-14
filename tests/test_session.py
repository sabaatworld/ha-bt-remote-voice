import pytest

from custom_components.bt_remote_voice.protocol import Control
from custom_components.bt_remote_voice.session import AudioFailure, VoiceSession


async def collect(session):
    return [chunk async for chunk in session.stream()]


async def test_release_drains_audio_then_eof():
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()
    assert [len(x) for x in await collect(s)] == [512]


async def test_overflow_fails_instead_of_executing_truncated_audio():
    s = VoiceSession(128, max_bytes=512)
    s.audio(bytes(128))
    s.audio(bytes(128))
    with pytest.raises(AudioFailure, match="buffer_overflow"):
        await collect(s)


async def test_first_audio_timeout_and_fresh_session():
    s = VoiceSession(128, first_timeout=0.01)
    with pytest.raises(AudioFailure, match="no_audio"):
        await collect(s)
    s = VoiceSession(128, first_timeout=1, finish_delay=0)
    s.audio(bytes(128))
    s.release()
    assert len(await collect(s)) == 1


async def test_malformed_frame_and_disconnect_abort_accepted_audio():
    for error in ("invalid_audio_frame", "disconnected"):
        s = VoiceSession(128)
        s.audio(bytes(128))
        if error == "invalid_audio_frame":
            s.audio(b"x")
        else:
            s.abort(error)
        with pytest.raises(AudioFailure, match=error):
            await collect(s)


async def test_sync_detects_missing_headerless_frames():
    s = VoiceSession(128)
    s.audio(bytes(128))
    s.sync(Control("sync", sequence=2, codec=2))
    with pytest.raises(AudioFailure, match="audio_sequence_gap"):
        await collect(s)


async def test_release_without_audio_does_not_succeed():
    s = VoiceSession(128, finish_delay=0)
    s.release()
    with pytest.raises(AudioFailure, match="no_audio"):
        await collect(s)
