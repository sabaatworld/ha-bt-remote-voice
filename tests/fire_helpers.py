"""Synthetic Fire-remote audio: 80-byte CBR Opus packets, TOC 0xB8 (16 kHz, 20 ms)."""

import numpy as np

SAMPLES = 320


def synthetic_packets(count=50, hz=440):
    import av

    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = 16000
    enc.layout = "mono"
    enc.format = "s16"
    enc.bit_rate = 32000
    enc.options = {"application": "lowdelay", "vbr": "off", "frame_duration": "20"}
    enc.open()
    t = np.arange(SAMPLES * (count + 1)) / 16000
    signal = (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16)
    packets = []
    for i in range(count + 1):
        frame = av.AudioFrame.from_ndarray(
            signal[i * SAMPLES : (i + 1) * SAMPLES].reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = 16000
        frame.pts = i * SAMPLES
        packets += [bytes(p) for p in enc.encode(frame)]
    packets += [bytes(p) for p in enc.encode(None)]
    return packets[:count]
