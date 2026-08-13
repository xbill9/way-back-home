"""mu-law round-trip properties.

A wrong table does not raise -- it sounds like static, or like silence, and the
only way to find out is to play it to someone. These pin the arithmetic so that
never has to be the test.
"""

import math
import struct

# conftest puts backend/app on sys.path, so this is a top-level import.
from audio_codec import pcm16_to_ulaw, ulaw_to_pcm16


def sine(seconds=0.2, freq=440.0, rate=24000, amplitude=12000):
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )


def samples_of(pcm):
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def test_encoding_halves_the_bytes():
    pcm = sine()
    assert len(pcm16_to_ulaw(pcm)) == len(pcm) // 2


def test_silence_stays_silent():
    """The failure that is easiest to ship: a table that renders quiet as loud."""
    pcm = b"\x00\x00" * 500
    decoded = samples_of(ulaw_to_pcm16(pcm16_to_ulaw(pcm)))
    assert max(abs(s) for s in decoded) < 200


def test_round_trip_keeps_the_waveform():
    """Lossy, but the error has to stay small relative to the signal.

    mu-law's whole point is logarithmic steps, so absolute error grows with
    amplitude; the meaningful bound is on the ratio, not on any one sample.
    """
    pcm = sine()
    original = samples_of(pcm)
    recovered = samples_of(ulaw_to_pcm16(pcm16_to_ulaw(pcm)))
    assert len(recovered) == len(original)

    signal = sum(s * s for s in original)
    noise = sum((a - b) ** 2 for a, b in zip(original, recovered, strict=True))
    snr_db = 10 * math.log10(signal / noise)
    assert snr_db > 30, f"round trip SNR only {snr_db:.1f} dB"


def test_sign_is_preserved():
    """Sign inversion is silent in a sine and obvious in speech."""
    pcm = struct.pack("<8h", 1000, -1000, 8000, -8000, 20000, -20000, 1, -1)
    recovered = samples_of(ulaw_to_pcm16(pcm16_to_ulaw(pcm)))
    for original, got in zip(samples_of(pcm), recovered, strict=True):
        if abs(original) > 100:
            assert (original > 0) == (got > 0), f"{original} came back {got}"


def test_full_scale_does_not_wrap():
    """Clipping must saturate, not wrap around into the opposite sign."""
    pcm = struct.pack("<4h", 32767, -32768, 32000, -32000)
    recovered = samples_of(ulaw_to_pcm16(pcm16_to_ulaw(pcm)))
    assert recovered[0] > 30000 and recovered[1] < -30000
    assert recovered[2] > 30000 and recovered[3] < -30000


def test_odd_length_input_is_not_a_crash():
    """Chunks arrive off a socket; a stray byte must not take the session down."""
    assert len(pcm16_to_ulaw(b"\x01\x02\x03")) == 1
