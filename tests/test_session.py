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


# ---- early STOP and short audio gaps (remote quirks, both off by default) ----

import asyncio as _asyncio  # noqa: E402

from custom_components.bt_remote_voice import session as _session_module  # noqa: E402


def _sync(sequence):
    return Control("sync", codec=2, sequence=sequence, predictor=0, index=0)


async def _frames(s, count):
    for _ in range(count):
        s.audio(bytes(128))


async def test_early_stop_is_held_back_only_when_the_setting_is_on_and_audio_continues(monkeypatch):
    monkeypatch.setattr(_session_module, "STOP_QUIET_SECONDS", 0.1)
    monkeypatch.setattr(_session_module, "LATE_AUDIO_SECONDS", 0.1)
    s = VoiceSession(128, finish_delay=0, ignore_early_stop=True)
    await _frames(s, 3)
    s.remote_stop()
    assert not s.released and s.stop_pending  # held back
    for _ in range(4):  # the remote keeps streaming for a while
        await _frames(s, 3)
        await _asyncio.sleep(0.05)
        assert not s.released
    await _asyncio.sleep(0.25)  # then it goes quiet
    assert s.released and s.early_stop_seen and s.frames == 15


async def test_early_stop_off_ends_at_the_first_stop_but_still_notices_the_stream_goes_on():
    s = VoiceSession(128, finish_delay=0)
    await _frames(s, 3)
    s.remote_stop()
    assert s.released and s.finished
    for _ in range(12):  # the remote keeps streaming for a while: ignored, but counted
        await _frames(s, 1)
        await _asyncio.sleep(0.05)
    assert s.frames == 3 and s.early_stop_seen


async def test_a_second_stop_ends_a_held_back_stop_at_once():
    s = VoiceSession(128, finish_delay=0, ignore_early_stop=True)
    await _frames(s, 3)
    s.remote_stop()
    s.remote_stop()
    assert s.released


async def test_a_late_stop_is_never_held_back(monkeypatch):
    monkeypatch.setattr(_session_module, "EARLY_STOP_WINDOW", 0.05)
    s = VoiceSession(128, finish_delay=0, ignore_early_stop=True)
    await _frames(s, 3)
    await _asyncio.sleep(0.1)
    s.remote_stop()
    assert s.released and not s.early_stop_seen


async def test_short_gap_is_covered_with_silence_when_allowed():
    s = VoiceSession(128, finish_delay=0, max_gap_ms=1000)
    await _frames(s, 5)
    frame_bytes = s.last_pcm_len
    s.sync(_sync(8))  # three frames never arrived
    assert s.error is None and s.sequence == 8 and s.gaps == 1 and s.lost_frames == 3
    assert s.gaps_covered == 1
    silence = b"".join(list(s.queue)[-1:])
    assert silence == bytes(frame_bytes * 3)


async def test_gap_is_not_covered_when_off_or_too_long():
    off = VoiceSession(128, finish_delay=0)
    await _frames(off, 5)
    off.sync(_sync(8))
    assert off.error == "audio_sequence_gap" and off.gaps == 1 and off.lost_frames == 3
    long = VoiceSession(128, finish_delay=0, max_gap_ms=20)  # one frame at most
    await _frames(long, 5)
    long.sync(_sync(8))
    assert long.error == "audio_sequence_gap"


async def test_a_frame_count_that_went_backwards_is_never_covered():
    s = VoiceSession(128, finish_delay=0, max_gap_ms=500)
    await _frames(s, 5)
    s.sync(_sync(2))
    assert s.error == "audio_sequence_gap"


async def test_a_burst_behind_a_quick_release_is_not_an_early_stop():
    for trailing in (9, 10, 30):  # many frames, but all within an instant
        s = VoiceSession(128, finish_delay=0)
        await _frames(s, 3)
        s.remote_stop()
        await _frames(s, trailing)
        assert not s.early_stop_seen, trailing


async def test_gap_arithmetic_across_the_sequence_wrap_and_at_the_limit():
    s = VoiceSession(128, finish_delay=0, max_gap_ms=1000)
    await _frames(s, 2)
    frame_ms = s.last_pcm_len / 32
    s.sequence = 65534  # the frame counter is about to wrap
    s.sync(_sync(1))  # frames 65534, 65535, 0 never arrived: three lost
    assert s.error is None and s.lost_frames == 3 and s.sequence == 1
    exact = VoiceSession(128, finish_delay=0, max_gap_ms=int(frame_ms) * 2)
    await _frames(exact, 2)
    exact.sync(_sync(4))  # exactly two frames lost: allowed
    assert exact.error is None
    over = VoiceSession(128, finish_delay=0, max_gap_ms=int(frame_ms) * 2)
    await _frames(over, 2)
    over.sync(_sync(5))  # three frames lost: over the limit
    assert over.error == "audio_sequence_gap"


async def test_a_gap_before_any_audio_is_never_covered():
    s = VoiceSession(128, finish_delay=0, max_gap_ms=500)
    s.sync(_sync(3))  # no frame yet, so the frame length is unknown
    assert s.error == "audio_sequence_gap"
