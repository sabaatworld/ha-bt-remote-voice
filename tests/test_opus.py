import ctypes.util
from unittest.mock import patch

import pytest

pytest.importorskip("av")

from custom_components.bt_remote_voice.opus import OpusDecoder, opus_available  # noqa: E402
from tests.fire_helpers import synthetic_packets  # noqa: E402


def _rms(pcm):
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(float)
    return float((samples**2).mean() ** 0.5)


def test_synthetic_packets_look_like_the_remote():
    packets = synthetic_packets(10)
    assert {len(p) for p in packets} == {80} and {p[0] for p in packets} == {0xB8}


def test_stream_decodes_to_expected_total_length():
    decoder = OpusDecoder()
    out = [decoder.decode(p) for p in synthetic_packets(50)]
    assert all(len(chunk) % 2 == 0 for chunk in out)
    total = sum(len(chunk) for chunk in out)
    assert abs(total - 50 * 640) <= 0.02 * 50 * 640
    assert _rms(b"".join(out)) > 1000  # real audio, not silence


@pytest.mark.parametrize("size", [0, 79, 81])
def test_wrong_length_is_invalid_opus_packet(size):
    with pytest.raises(ValueError, match="invalid_opus_packet"):
        OpusDecoder().decode(bytes(size))


def test_corrupt_packet_never_crashes():
    decoder = OpusDecoder()
    for packet in (bytes(80), bytes([0xB8]) + bytes([0xFF] * 79), bytes(range(80))):
        try:
            assert isinstance(decoder.decode(packet), bytes)
        except ValueError as err:
            assert str(err) == "invalid_opus_packet"


def test_decoder_unavailable_is_reported():
    with (
        patch("custom_components.bt_remote_voice.opus._PyAvBackend", side_effect=ImportError),
        patch("custom_components.bt_remote_voice.opus._CtypesBackend", side_effect=OSError),
    ):
        with pytest.raises(ValueError, match="opus_decoder_unavailable"):
            OpusDecoder()
        assert opus_available() is False


@pytest.mark.skipif(ctypes.util.find_library("opus") is None, reason="no system libopus")
def test_ctypes_fallback_decodes_same_stream_approximately():
    packets = synthetic_packets(50)
    with patch("custom_components.bt_remote_voice.opus._PyAvBackend", side_effect=ImportError):
        fallback = OpusDecoder()
    primary = OpusDecoder()
    a = b"".join(primary.decode(p) for p in packets)
    b = b"".join(fallback.decode(p) for p in packets)
    assert abs(len(a) - len(b)) <= 0.02 * len(b)
    assert abs(_rms(a) - _rms(b)) <= 0.1 * _rms(b)
