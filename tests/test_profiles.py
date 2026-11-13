from array import array

import pytest

from custom_components.atvv_remote.protocol import Capabilities, capabilities, parse_control
from custom_components.atvv_remote.session import AudioFailure, VoiceSession


def test_upstream_capability_vectors_and_versioned_commands():
    legacy = capabilities(bytes.fromhex("0b0004000100860014"))
    assert (legacy.version, legacy.codec, legacy.sample_rate, legacy.model) == (4, 1, 8000, 0)
    assert legacy.open_command() == bytes.fromhex("0c0001")
    assert legacy.close_command(7) == b"\x0d"
    assert legacy.keepalive_command(7) == bytes.fromhex("0c0001")
    modern = capabilities(bytes.fromhex("0b010003010080"))
    assert (modern.version, modern.codec, modern.model) == (256, 2, 1)
    assert modern.open_command() == bytes.fromhex("0c00")
    assert modern.close_command(7) == bytes.fromhex("0d07")
    assert modern.keepalive_command(7) == bytes.fromhex("0e07")
    for raw in ("0b020003030080", "0b010004030080", "0b010003020080", "0b0004000100060014"):
        with pytest.raises(ValueError):
            capabilities(bytes.fromhex(raw))


def test_legacy_controls_have_no_payload():
    c = Capabilities(134, version=4, codec=1, model=0)
    assert parse_control(b"\x04", c).codec == 1
    assert parse_control(b"\x00", c).kind == "stop"
    assert parse_control(bytes.fromhex("0a1234"), c).sequence == 0x1234


async def test_legacy_frame_header_decoder_and_8k_normalization():
    caps = capabilities(bytes.fromhex("0b0004000100080014"))
    s = VoiceSession(caps.frame_size, caps=caps, finish_delay=0)
    s.audio(bytes.fromhex("ffff000000001717"))
    s.audio(bytes.fromhex("0000000000001717"))
    s.release()
    chunks = [x async for x in s.stream()]
    assert len(chunks) == 2
    assert len(chunks[0]) == 20  # 5 samples at 8k become 10 samples at 16k
    assert list(array("h", chunks[0]))[-1] == 43
    assert not s.error


async def test_legacy_frame_gap_and_invalid_index_abort():
    caps = Capabilities(8, version=4, codec=1, model=0)
    for second in (bytes.fromhex("0002000000001717"), bytes.fromhex("0001000000591717")):
        s = VoiceSession(8, caps=caps)
        s.audio(bytes.fromhex("0000000000001717"))
        s.audio(second)
        with pytest.raises(AudioFailure):
            _ = [x async for x in s.stream()]


async def test_sync_mismatch_records_anonymous_frame_counts():
    s = VoiceSession(128)
    s.audio(bytes(128))
    s.sync(parse_control(bytes.fromhex("0a020002000001")))
    assert s.error == "audio_sequence_gap"
    assert s.sequence_mismatch == {"expected": 2, "received": 1}


async def test_tap_capture_ends_on_speech_then_silence_before_intent():
    class Vad:
        probability = 0.9

        def Process10ms(self, chunk):
            assert len(chunk) == 320
            return self.probability

    s = VoiceSession(128, caps=Capabilities(128, model=1))
    s.vad = Vad()
    for _ in range(45):
        s.audio(bytes(128))
    assert not s.finished
    s.vad.probability = 0
    for _ in range(55):
        s.audio(bytes(128))
    assert s.finished and s.released and not s.error
    assert [x async for x in s.stream()]
    assert s.input_complete


async def test_tap_no_speech_never_completes_successfully():
    s = VoiceSession(128, caps=Capabilities(128, model=1))
    s.segmenter.timeout_seconds = 0.01
    s.segmenter.reset()
    s.audio(bytes(128))
    assert s.error == "no_speech"


async def test_future_sync_cannot_hide_dropped_rx_followed_by_a_later_packet():
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.sync(parse_control(bytes.fromhex("0a020002000001")))
    # Frame 2 was lost; frame 3 arrives. Headerless RX cannot identify this.
    s.audio(bytes(128))
    s.release()
    with pytest.raises(AudioFailure, match="audio_sequence_gap"):
        _ = [x async for x in s.stream()]
