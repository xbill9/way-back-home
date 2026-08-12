"""Shared fixtures for the hermetic suite.

Nothing here touches the network or needs an API key. `main.py` only calls out
to Gemini inside `runner.run_live()`; every other line of the WebSocket endpoint
is reachable with that one call stubbed, so the whole protocol layer is testable
offline. (`main.py`'s missing-key `sys.exit(1)` is guarded by
`if __name__ == "__main__"`, so importing it without a key is fine.)
"""

import asyncio
import os
import sys

import pytest

BACKEND_APP = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "backend", "app")
)
if BACKEND_APP not in sys.path:
    sys.path.insert(0, BACKEND_APP)


@pytest.fixture(scope="session")
def main_module():
    """The real FastAPI app, imported once."""
    import main

    return main


class SpyQueue:
    """Stands in for LiveRequestQueue and records what the endpoint sends upstream.

    Deliberately mirrors the post-ADK-2.6.3 contract: `send_realtime` takes only
    a `types.Blob`, text goes through `send_content`. Keeping them as separate
    lists is what lets a test assert that text never leaked into `send_realtime`.
    """

    def __init__(self):
        self.realtime = []
        self.content = []
        self.closed = False

    def send_realtime(self, blob):
        self.realtime.append(blob)

    def send_content(self, content):
        self.content.append(content)

    def close(self):
        self.closed = True

    @property
    def audio_blobs(self):
        return [b for b in self.realtime if b.mime_type.startswith("audio/")]

    @property
    def image_blobs(self):
        return [b for b in self.realtime if b.mime_type.startswith("image/")]

    @property
    def texts(self):
        return [p.text for c in self.content for p in c.parts if p.text]


@pytest.fixture
def spy(main_module, monkeypatch):
    """Swap in the spy queue and stub the one call that would hit the network."""
    queue = SpyQueue()
    monkeypatch.setattr(main_module, "LiveRequestQueue", lambda *a, **k: queue)

    async def no_events(**kwargs):
        # Stays open without yielding, which is what a real `run_live` does
        # between model turns. An immediate `return` here would instead mean
        # "the Live API hung up", and the endpoint would (correctly) tear the
        # session down before the client's frames were ever read.
        await asyncio.Event().wait()
        yield  # unreachable; makes this an async generator

    monkeypatch.setattr(main_module.runner, "run_live", no_events)
    return queue


@pytest.fixture
def ws_connect(main_module):
    """Open the real endpoint over an in-process transport.

    This used to suppress exceptions on teardown, because the endpoint awaited
    `asyncio.gather(upstream_task(), downstream_task(), heartbeat_task())` and
    `heartbeat_task` looped forever, so the gather never completed and the test
    portal cancelled it. That suppression was documented as the canary for the
    shutdown fix; the endpoint now waits on the two lifecycle tasks with
    FIRST_COMPLETED and cancels the rest, so teardown is clean and the
    suppression is gone. If you find yourself wanting it back, the endpoint has
    regressed -- fix that instead.
    """
    import contextlib

    from starlette.testclient import TestClient

    @contextlib.contextmanager
    def _connect(path="/ws/u1/s1", headers=None):
        # Only pass headers when given: TestClient calls headers.setdefault(),
        # so an explicit None raises AttributeError.
        kwargs = {"headers": headers} if headers is not None else {}
        cm = TestClient(main_module.app).websocket_connect(path, **kwargs)
        ws = cm.__enter__()
        try:
            yield ws
        finally:
            cm.__exit__(None, None, None)

    return _connect
