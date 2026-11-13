"""ATVV v1.0 controls and IMA/DVI ADPCM (see THIRD_PARTY_NOTICES.md)."""

import sys
from array import array
from dataclasses import dataclass

SERVICE = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
TX, RX, CTL = (f"ab5e000{i}-5a21-4f05-bc7d-af01f617b664" for i in (2, 3, 4))
GET_CAPS = bytes.fromhex("0a0100000303")
STEP = (
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
)
INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)


@dataclass(frozen=True)
class Capabilities:
    frame_size: int


@dataclass(frozen=True)
class Control:
    kind: str
    stream_id: int = 0
    reason: int = 0
    codec: int = 2
    sequence: int = 0
    predictor: int = 0
    index: int = 0


def capabilities(data: bytes) -> Capabilities:
    """Accept the verified v1.0, 16 kHz, hold-to-talk profile only."""
    if len(data) < 7 or data[:3] != b"\x0b\x01\x00" or not data[3] & 2 or data[4] != 3:
        raise ValueError("unsupported_capabilities")
    size = int.from_bytes(data[5:7], "big")
    if not 1 <= size <= 512:
        raise ValueError("invalid_frame_size")
    return Capabilities(size)


def parse_control(data: bytes) -> Control | None:
    if not data:
        return None
    if data[0] == 4 and len(data) >= 4:
        return Control("start", stream_id=data[3], reason=data[1], codec=data[2])
    if data[0] == 0 and len(data) >= 2:
        return Control("stop", reason=data[1])
    if data[0] == 8:
        return Control("search")
    if data[0] == 10 and len(data) >= 7:
        return Control(
            "sync",
            codec=data[1],
            sequence=int.from_bytes(data[2:4], "big"),
            predictor=int.from_bytes(data[4:6], "big", signed=True),
            index=data[6],
        )
    return None


class AdpcmDecoder:
    def __init__(self):
        self.reset(0, 0)

    def reset(self, predictor: int, index: int):
        self.predictor = max(-32768, min(32767, predictor))
        self.index = max(0, min(88, index))

    def decode(self, data: bytes) -> list[int]:
        samples = []
        for byte in data:
            for nibble in (byte >> 4, byte & 15):
                step = STEP[self.index]
                diff = (
                    (step >> 3)
                    + ((step >> 2) if nibble & 1 else 0)
                    + ((step >> 1) if nibble & 2 else 0)
                    + (step if nibble & 4 else 0)
                )
                self.predictor = max(
                    -32768, min(32767, self.predictor + (-diff if nibble & 8 else diff))
                )
                self.index = max(0, min(88, self.index + INDEX[nibble & 7]))
                samples.append(self.predictor)
        return samples


def decode_frame(decoder: AdpcmDecoder, data: bytes, size: int) -> bytes:
    if len(data) != size:
        raise ValueError("invalid_audio_frame")
    pcm = array("h", decoder.decode(data))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()
