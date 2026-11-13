import pytest

from custom_components.atvv_remote.protocol import (
    AdpcmDecoder,
    capabilities,
    decode_frame,
    parse_control,
)


def test_adpcm_high_nibble_first_and_state_continuity():
    d = AdpcmDecoder()
    assert d.decode(bytes.fromhex("1717")) == [1, 12, 18, 43]
    assert d.decode(bytes.fromhex("00")) == [46, 49]


def test_decoder_reset_clamps_step_index_and_saturates():
    d = AdpcmDecoder()
    d.reset(32760, 255)
    assert d.decode(b"\x77") == [32767, 32767]
    d.reset(0, 0)
    assert d.decode(b"\x00") == [0, 0]


def test_caps_validated_and_trailing_bytes_allowed():
    c = capabilities(bytes.fromhex("0b0100030300800100"))
    assert c.frame_size == 128
    for raw in ["0b010000030080", "0b010003030000", "0b01"]:
        with pytest.raises(ValueError):
            capabilities(bytes.fromhex(raw))


def test_controls_reject_unknown_or_short_messages():
    assert parse_control(bytes.fromhex("04030207")).stream_id == 7
    assert parse_control(bytes.fromhex("0002")).kind == "stop"
    assert parse_control(bytes.fromhex("0800")).kind == "search"
    for raw in [b"", b"\x04", b"\x0a\x02", b"\xff"]:
        assert parse_control(raw) is None


def test_sync_and_frame_size_are_enforced():
    e = parse_control(bytes.fromhex("0a020014fff910"))
    assert (e.sequence, e.predictor, e.index) == (20, -7, 16)
    d = AdpcmDecoder()
    with pytest.raises(ValueError):
        decode_frame(d, b"\x00", 128)
    assert len(decode_frame(d, bytes(128), 128)) == 512
