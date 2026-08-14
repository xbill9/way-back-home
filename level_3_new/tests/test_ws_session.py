"""Session-level behaviour: handshake policy, shutdown, and the audio rate contract.

`test_ws_protocol.py` covers the frame-by-frame wire contract. This file covers
the things that only show up across a whole connection.
"""

import json
import struct

import pytest


def test_config_frame_carries_the_binary_prefixes(spy, ws_connect, main_module):
    """The client adopts these at runtime instead of hardcoding its own copy.

    Without them in the frame, `useGeminiSocket.js` silently falls back to its
    literals and the two ends can drift apart again.
    """
    with ws_connect() as ws:
        msg = json.loads(ws.receive_text())

    assert msg["audio_prefix"] == main_module.AUDIO_PREFIX
    assert msg["jpeg_prefix"] == main_module.JPEG_PREFIX
    assert msg["input_sample_rate"] == main_module.DEFAULT_INPUT_SAMPLE_RATE


def test_no_synthetic_model_turn_before_the_model_speaks(spy, ws_connect):
    """The endpoint used to open with mock/mock_audio.pcm dressed up as a model turn.

    A canned recording wrapped in serverContent.modelTurn is indistinguishable
    from real model output to the client, which makes it a lie in a demo whose
    whole point is that the Live API is talking. The config frame must be the
    only thing sent before the model produces something.
    """
    with ws_connect() as ws:
        first = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "text", "text": "probe"}))

    assert first["type"] == "config"
    assert "serverContent" not in first


def test_client_reported_rate_is_used_to_label_audio(spy, ws_connect):
    """A browser that refuses 16 kHz must not have its audio mislabelled."""
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "audio_config", "sample_rate": 48000}))
        ws.send_bytes(b"\x01" + b"\x00\x01" * 8)

    assert spy.audio_blobs[0].mime_type == "audio/pcm;rate=48000"


def test_rate_defaults_to_16k_without_an_audio_config(spy, ws_connect):
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_bytes(b"\x01" + b"\x00\x01" * 8)

    assert spy.audio_blobs[0].mime_type == "audio/pcm;rate=16000"


def test_bogus_rate_is_ignored(spy, ws_connect):
    """A malformed audio_config must not poison every subsequent blob."""
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "audio_config", "sample_rate": "fast"}))
        ws.send_text(json.dumps({"type": "audio_config", "sample_rate": -1}))
        ws.send_bytes(b"\x01" + b"\x00\x01" * 8)

    assert spy.audio_blobs[0].mime_type == "audio/pcm;rate=16000"


def test_disallowed_origin_is_refused(spy, ws_connect, monkeypatch, main_module):
    """CORS does not apply to WebSockets; this check is the only gate."""
    monkeypatch.setattr(main_module, "ALLOWED_ORIGINS", ["https://demo.example"])

    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        ws_connect(headers={"origin": "https://evil.example"}) as ws,
    ):
        ws.receive_text()

    assert excinfo.value.code == 1008
    # Rejected before the queue was ever built.
    assert not spy.realtime


def test_allowed_origin_gets_through(spy, ws_connect, monkeypatch, main_module):
    monkeypatch.setattr(main_module, "ALLOWED_ORIGINS", ["https://demo.example"])

    with ws_connect(headers={"origin": "https://demo.example"}) as ws:
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "config"


def test_empty_allowlist_accepts_any_origin(spy, ws_connect, monkeypatch, main_module):
    """The local-development default, kept explicit so it can't regress silently."""
    monkeypatch.setattr(main_module, "ALLOWED_ORIGINS", [])

    with ws_connect(headers={"origin": "https://anything.example"}) as ws:
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "config"


def test_system_error_does_not_write_to_the_closed_socket(
    main_module, monkeypatch, ws_connect, caplog
):
    """`trigger_system_error` closes the socket, then must stop touching it.

    It used to `break`, which only left the `for fc` loop -- the enclosing
    `async for event` carried on to `send_text(event_json)` and logged
    'Cannot call "send" once a close message has been sent.' on every trigger.
    """
    from google.adk.events import Event
    from google.genai import types

    event = Event(
        author="biometric_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="trigger_system_error", args={}
                    )
                )
            ],
        ),
    )

    class Q:
        def send_realtime(self, blob):
            pass

        def send_content(self, content):
            pass

        def close(self):
            pass

    monkeypatch.setattr(main_module, "LiveRequestQueue", lambda *a, **k: Q())

    async def one_event(**kwargs):
        yield event

    monkeypatch.setattr(main_module.runner, "run_live", one_event)

    with ws_connect() as ws:
        ws.receive_text()  # config
        assert json.loads(ws.receive_text())["type"] == "system_error"

    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []


def test_session_ends_when_the_client_disconnects(spy, ws_connect):
    """The shutdown fix, stated as a test.

    Under the old `asyncio.gather(upstream, downstream, heartbeat)` the gather
    never completed on a clean disconnect -- heartbeat_task loops forever -- so
    the `finally` never ran and the billed Live session stayed open. The queue
    closing is the observable end of the session.
    """
    with ws_connect() as ws:
        ws.receive_text()

    assert spy.closed


def test_model_audio_is_sent_as_binary_mulaw_not_base64(
    main_module, monkeypatch, ws_connect
):
    """Model audio rides a prefixed binary frame, mu-law encoded, and only there.

    Base64 inside the event JSON inflated binary by a third: over a 14s session
    the downlink was 220KB of which 202KB was audio. As mu-law on its own frame
    the same audio is 77KB. Both regressions this guards are silent -- sending
    it twice doubles the cost with nothing looking wrong, and dropping the frame
    leaves the demo mute with nothing in the log.
    """
    from audio_codec import pcm16_to_ulaw, ulaw_to_pcm16
    from google.adk.events import Event
    from google.genai import types

    monkeypatch.setattr(main_module, "MODEL_AUDIO_ENCODING", "mulaw")
    pcm = struct.pack("<8h", 0, 4000, -4000, 12000, -12000, 800, -800, 0)

    class Q:
        def send_realtime(self, blob):
            pass

        def send_content(self, content):
            pass

        def close(self):
            pass

    monkeypatch.setattr(main_module, "LiveRequestQueue", lambda *a, **k: Q())

    async def one_turn(**kwargs):
        yield Event(
            author="biometric_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="audio/pcm;rate=24000", data=pcm
                        )
                    ),
                    types.Part(text="Scanner Online."),
                ],
            ),
        )

    monkeypatch.setattr(main_module.runner, "run_live", one_turn)

    with ws_connect() as ws:
        config = json.loads(ws.receive_text())
        assert config["model_audio_encoding"] == "mulaw"
        assert config["model_audio_prefix"] == main_module.MODEL_AUDIO_PREFIX
        assert config["model_audio_rate"] == 24000

        frame = ws.receive_bytes()
        assert frame[0] == main_module.MODEL_AUDIO_PREFIX
        assert frame[1:] == pcm16_to_ulaw(pcm)
        # Half the bytes, one per sample, which is the entire point.
        assert len(frame) - 1 == len(pcm) // 2

        # And it still sounds like the original: lossy, but not wrong.
        recovered = struct.unpack("<8h", ulaw_to_pcm16(frame[1:]))
        for original, got in zip(struct.unpack("<8h", pcm), recovered, strict=True):
            assert abs(original - got) <= max(64, abs(original) * 0.1)

        forwarded = json.loads(ws.receive_text())
        assert not any(
            part.get("inlineData")
            for part in (forwarded.get("content") or {}).get("parts") or []
        ), "audio must not also be sent inside the JSON"
        assert any(
            part.get("text") == "Scanner Online."
            for part in (forwarded.get("content") or {}).get("parts") or []
        ), "non-audio parts must survive the strip"


def _digit_events(n, count=3):
    """`n` `report_digit(count)` calls, as the model would emit them."""
    from google.adk.events import Event
    from google.genai import types

    return [
        Event(
            author="biometric_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="report_digit", args={"count": count}
                        )
                    )
                ],
            ),
        )
        for _ in range(n)
    ]


def _nudges(spy):
    return [t for t in spy.texts if "Stop calling report_digit" in t]


def test_a_repeat_run_is_nudged_once(main_module, monkeypatch, ws_connect, spy):
    """A scan answered by a run of calls gets one new user turn, not one per call.

    The failure this catches is invisible from outside: every call is answered
    correctly and the digit still reaches the UI, so the only symptom is that
    the scanner never speaks. A new user turn is the one thing measured to end
    such a run -- and it has to be exactly one, or the rescue becomes the loop.
    """
    monkeypatch.setattr(main_module, "STORM_NUDGE_AFTER", 3)

    async def storm(**kwargs):
        for event in _digit_events(6):
            yield event

    monkeypatch.setattr(main_module.runner, "run_live", storm)

    with ws_connect() as ws:
        ws.receive_text()  # config

    assert _nudges(spy) == [
        "3 is recorded. Stop calling report_digit and say the confirmation now."
    ]


def test_a_normal_scan_is_not_nudged(main_module, monkeypatch, ws_connect, spy):
    """Below the threshold nothing is sent.

    This model answers a scan with a single call and speaks on its own, so the
    nudge must stay out of the way of the case that actually happens. A turn per
    scan would be a turn nobody needed, on every scan.
    """
    monkeypatch.setattr(main_module, "STORM_NUDGE_AFTER", 3)

    async def two_calls(**kwargs):
        for event in _digit_events(2):
            yield event

    monkeypatch.setattr(main_module.runner, "run_live", two_calls)

    with ws_connect() as ws:
        ws.receive_text()  # config

    assert _nudges(spy) == []
