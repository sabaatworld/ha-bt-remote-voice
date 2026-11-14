"""Opus decoding for remotes that stream 80-byte packets (16 kHz mono, 20 ms).

Home Assistant pins PyAV, whose wheel bundles libopus, so that is the primary
backend; the system libopus through ctypes is the fallback. Adapter errors are
plain ``ValueError`` codes: ``opus_decoder_unavailable`` and ``invalid_opus_packet``.
"""

import ctypes
import ctypes.util
import struct

PACKET_BYTES = 80
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms


class _PyAvBackend:
    def __init__(self):
        import av

        self._av = av
        self._ctx = av.CodecContext.create("libopus", "r")
        self._ctx.sample_rate = SAMPLE_RATE
        self._ctx.layout = "mono"
        # OpusHead: version 1, 1 channel, pre-skip 0 (keep every packet's audio).
        self._ctx.extradata = (
            b"OpusHead" + bytes([1, 1]) + struct.pack("<HIhB", 0, SAMPLE_RATE, 0, 0)
        )
        self._ctx.open()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def decode(self, packet):
        pcm = bytearray()
        for frame in self._ctx.decode(self._av.Packet(packet)):
            for out in self._resampler.resample(frame):
                pcm += bytes(out.planes[0])[: out.samples * 2]
        return bytes(pcm)


class _CtypesBackend:
    def __init__(self):
        path = ctypes.util.find_library("opus") or "libopus.so.0"
        self._lib = ctypes.CDLL(path)
        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._lib.opus_decode.restype = ctypes.c_int
        self._lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        error = ctypes.c_int(0)
        self._decoder = self._lib.opus_decoder_create(SAMPLE_RATE, 1, ctypes.byref(error))
        if error.value != 0 or not self._decoder:
            raise OSError("opus_decoder_create failed")

    def decode(self, packet):
        buffer = (ctypes.c_int16 * FRAME_SAMPLES)()
        samples = self._lib.opus_decode(
            self._decoder, packet, len(packet), buffer, FRAME_SAMPLES, 0
        )
        if samples < 0:
            raise ValueError("opus_decode failed")
        return ctypes.string_at(buffer, samples * 2)


class OpusDecoder:
    def __init__(self):
        self._backend = None
        for backend in (_PyAvBackend, _CtypesBackend):
            try:
                self._backend = backend()
                break
            except Exception:  # noqa: BLE001 - try the next backend
                continue
        if self._backend is None:
            raise ValueError("opus_decoder_unavailable")

    def decode(self, packet):
        """Return s16le mono 16 kHz PCM; early packets may be shorter than 640 bytes."""
        if len(packet) != PACKET_BYTES:
            raise ValueError("invalid_opus_packet")
        try:
            return self._backend.decode(packet)
        except Exception as err:  # noqa: BLE001 - any decoder failure is a bad packet
            raise ValueError("invalid_opus_packet") from err


def opus_available():
    try:
        OpusDecoder()
    except ValueError:
        return False
    return True
