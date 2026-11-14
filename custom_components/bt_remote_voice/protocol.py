"""ATVV v0.4/v1.0 controls and IMA/DVI ADPCM (see THIRD_PARTY_NOTICES.md)."""

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
    version: int = 256
    codec: int = 2
    model: int = 3
    family: str = "atvv"

    @property
    def sample_rate(self):
        return 16000 if self.codec in (2, 3) else 8000

    @property
    def profile(self):
        return f"{self.family}_{self.version:04x}_{self.sample_rate}_{self.model}"

    def open_command(self):
        return bytes([12, 0, self.codec]) if self.version == 4 else bytes([12, 0])

    def close_command(self, stream_id):
        return bytes([13]) if self.version == 4 else bytes([13, stream_id])

    def keepalive_command(self, stream_id):
        return self.open_command() if self.version == 4 else bytes([14, stream_id])


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
    """Negotiate only wire formats with a documented decoder."""
    if len(data) < 7 or data[0] != 11:
        raise ValueError("unsupported_capabilities")
    version = int.from_bytes(data[1:3], "big")
    if version == 4 and len(data) >= 9:
        codecs, model = int.from_bytes(data[3:5], "big"), 0
    elif version == 256 and data[4] in (0, 1, 3):
        codecs, model = data[3], data[4]
    else:
        raise ValueError("unsupported_capabilities")
    codec = 2 if codecs & 2 else 1 if codecs & 1 else 0
    if not codec:
        raise ValueError("unsupported_codec")
    size = int.from_bytes(data[5:7], "big")
    if not (7 if version == 4 else 1) <= size <= 512:
        raise ValueError("invalid_frame_size")
    return Capabilities(size, version, codec, model)


def parse_control(data: bytes, caps: Capabilities | None = None) -> Control | None:
    if not data:
        return None
    if caps and caps.version == 4:
        if data[0] == 4:
            return Control("start", codec=caps.codec)
        if data[0] == 0:
            return Control("stop")
        if data[0] == 10 and len(data) >= 3:
            return Control("sync", codec=caps.codec, sequence=int.from_bytes(data[1:3], "big"))
    if data[0] == 4 and len(data) >= 4:
        return Control("start", stream_id=data[3], reason=data[1], codec=data[2])
    if data[0] == 0 and len(data) >= 2:
        return Control("stop", reason=data[1])
    if data[0] == 8:
        return Control("search")
    if data[0] == 12 and len(data) >= 3:
        return Control("open_error", reason=int.from_bytes(data[1:3], "big"))
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
