"""G.711 mu-law, for the model audio on its way to the browser.

The Live API sends 24 kHz 16-bit PCM and offers no choice about it -- there is
no encoding field in LiveConnectConfig. The hop from this backend to the browser
is ours though, and it was the worst available encoding: base64 inside the event
JSON, which inflates binary by a third. Measured over a 14s session, 202KB of a
220KB downlink was base64 audio.

mu-law halves it again, for one byte per sample instead of two, and costs
nothing to adopt: no system library, no wheel, no WebCodecs. Opus would do far
better (measured 9.6% of PCM via PyAV, and Chrome decodes it happily) but wants
a 103MB wheel in an image whose dependency install is already delicate.

It is lossy -- 8-bit companded, so a raised noise floor against 16-bit PCM --
but it is the codec telephony ran on for decades, and the scanner says
"Two digits." rather than performing opera.

No stdlib help here: `audioop` was removed in Python 3.13.
"""

import array

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    """One signed 16-bit sample to one mu-law byte."""
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS

    # Exponent is the position of the highest set bit above the bias, which is
    # what gives mu-law its logarithmic resolution: fine steps near silence,
    # coarse ones near full scale, matching how hearing works.
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_sample(value: int) -> int:
    """One mu-law byte back to a signed 16-bit sample."""
    value = ~value & 0xFF
    magnitude = ((value & 0x0F) << 3) + _BIAS
    magnitude <<= (value & 0x70) >> 4
    magnitude -= _BIAS
    return -magnitude if value & 0x80 else magnitude


# Built once at import: 65536 entries indexed by the unsigned view of the
# sample, so encoding a chunk is a table lookup per sample rather than the
# branchy routine above. Costs a few milliseconds at startup and makes the
# per-chunk cost negligible against a 24 kHz stream.
_ENCODE_TABLE = bytes(
    _encode_sample(value if value < 0x8000 else value - 0x10000)
    for value in range(0x10000)
)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Little-endian signed 16-bit PCM to mu-law. Exactly half the bytes."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if samples.itemsize != 2:  # pragma: no cover - platform sanity
        raise RuntimeError("array('h') is not 16-bit on this platform")
    import sys

    if sys.byteorder == "big":  # pragma: no cover - not our target
        samples.byteswap()
    table = _ENCODE_TABLE
    return bytes(table[sample & 0xFFFF] for sample in samples)


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    """Inverse, for tests and for anything that needs to hear what was sent."""
    out = array.array("h", (_decode_sample(byte) for byte in ulaw))
    import sys

    if sys.byteorder == "big":  # pragma: no cover - not our target
        out.byteswap()
    return out.tobytes()
