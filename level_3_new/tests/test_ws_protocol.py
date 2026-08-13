"""The wire contract between the browser and the endpoint.

Binary frames carry a 1-byte prefix: 1 = audio (16 kHz PCM in), 2 = JPEG.
The client half lives in `frontend/src/useGeminiSocket.js` (`packet[0] = 1` /
`= 2`); the two ends share no constant, so these tests are the only thing
holding them together.
"""

import json


def test_config_frame_arrives_first(spy, ws_connect, main_module):
    """The client sizes its capture loop from this frame, so it must lead."""
    with ws_connect() as ws:
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "config"
    assert msg["video_fps"] == main_module.VIDEO_FPS
    assert msg["frame_interval_ms"] == main_module.FRAME_INTERVAL_MS
    assert msg["heartbeat_interval"] == main_module.HEARTBEAT_INTERVAL

    # Capture size and quality ride the same frame. The client falls back to
    # 640x480 q60 without them, which is 128 kbit/s of uplink instead of 77 --
    # a silent 1.7x regression, since nothing else would look wrong.
    assert msg["video_width"] == main_module.VIDEO_WIDTH
    assert msg["video_height"] == main_module.VIDEO_HEIGHT
    assert msg["jpeg_quality"] == main_module.JPEG_QUALITY

    # The UI titles itself with this. Without it the header reads
    # "AWAITING LINK" for the whole session and nothing else looks wrong.
    assert msg["model"] == main_module.MODEL_ID


def test_prefix_1_routes_to_16khz_audio(spy, ws_connect):
    payload = b"\xff\xfe" * 8
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_bytes(b"\x01" + payload)

    assert len(spy.audio_blobs) == 1
    assert spy.audio_blobs[0].mime_type == "audio/pcm;rate=16000"
    assert spy.audio_blobs[0].data == payload
    assert not spy.image_blobs


def test_prefix_2_routes_to_jpeg(spy, ws_connect):
    payload = b"\xff\xd8\xff\xe0jpeg"
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_bytes(b"\x02" + payload)

    assert len(spy.image_blobs) == 1
    assert spy.image_blobs[0].mime_type == "image/jpeg"
    assert spy.image_blobs[0].data == payload
    assert not spy.audio_blobs


def test_header_only_frame_is_dropped(spy, ws_connect):
    """A prefix with no payload must not become an empty blob."""
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_bytes(b"\x01")
        ws.send_bytes(b"\x02")

    assert spy.realtime == []


def test_unknown_prefix_is_ignored(spy, ws_connect):
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_bytes(b"\x09payload")

    assert spy.realtime == []


def test_malformed_json_does_not_drop_the_connection(spy, ws_connect):
    """One bad frame from the client must not end the session."""
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_text("{not json")
        ws.send_text(json.dumps({"type": "text", "text": "still alive"}))

    assert "still alive" in spy.texts


def test_text_never_reaches_send_realtime(spy, ws_connect):
    """ADK 2.6.3's send_realtime() takes only types.Blob.

    Text has to go through send_content(); a bare string raises a Pydantic
    ValidationError. This is the regression the removed patch_adk.py used to
    mask, so it is worth pinning.

    The string must be unique: main.py sends "Neural handshake" unconditionally
    at connect, so asserting on that phrase passes even if the client-text branch
    is deleted entirely.
    """
    sentinel = "sentinel-client-text-9f2b"
    with ws_connect() as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "text", "text": sentinel}))

    assert all(hasattr(b, "mime_type") for b in spy.realtime)
    assert sentinel in spy.texts


def test_queue_is_closed_on_disconnect(spy, ws_connect):
    """Phase 4 teardown: the queue must close even when tasks are cancelled."""
    with ws_connect() as ws:
        ws.receive_text()

    assert spy.closed
