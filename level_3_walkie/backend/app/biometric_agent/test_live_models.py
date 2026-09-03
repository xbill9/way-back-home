"""Model-selection rules for the Live audio EAP.

Hermetic: every function here is pure apart from reading MODEL_ID, so nothing
in this file opens a socket or needs a key. What it cannot check is whether the
EAP endpoints actually accept what these rules produce -- `make test` stubs
`run_live()`, so the wire format is only ever verified against the real API by
hand. See CLAUDE.md.
"""

import os
import sys
import unittest
from unittest import mock

from google.adk.models.google_llm import Gemini
from google.genai import types

from .live_models import (
    CLEVER_CHATTER,
    CLI_FALLBACK_MODEL,
    DEFAULT_MODEL_ID,
    WALKIE_TALKIE,
    EapLiveGemini,
    build_generate_content_config,
    build_live_model,
    get_model_id,
    is_eap_model,
    normalize_model_id,
    supports_blocking_function_calls,
    supports_thinking,
)

PRE_EAP = "gemini-3.1-flash-live-preview"


class TestModelIdResolution(unittest.TestCase):
    def test_default_is_walkie_talkie(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(sys, "argv", ["pytest"]),
        ):
            os.environ.pop("MODEL_ID", None)
            self.assertEqual(get_model_id(), WALKIE_TALKIE)
        self.assertEqual(DEFAULT_MODEL_ID, WALKIE_TALKIE)

    def test_bare_eap_name_gets_the_models_prefix(self):
        """The API wants models/walkie-talkie; a bare name 404s at connect."""
        self.assertEqual(normalize_model_id("walkie-talkie"), WALKIE_TALKIE)
        self.assertEqual(normalize_model_id('"clever-chatter"'), CLEVER_CHATTER)
        self.assertEqual(normalize_model_id(" gemini-2.5-flash "), "gemini-2.5-flash")

    def test_env_override_is_respected(self):
        with (
            mock.patch.dict(os.environ, {"MODEL_ID": "gemini-2.5-flash"}),
            mock.patch.object(sys, "argv", ["pytest"]),
        ):
            self.assertEqual(get_model_id(), "gemini-2.5-flash")

    def test_adk_run_falls_back_off_every_live_only_model(self):
        """`adk run` drives generateContent, which Live-only models 404 on.

        This used to key off one hardcoded model name, so an explicit
        MODEL_ID=<some other live model> sailed past it into a 404.
        """
        for model in (WALKIE_TALKIE, CLEVER_CHATTER, PRE_EAP):
            with (
                self.subTest(model=model),
                mock.patch.dict(os.environ, {"MODEL_ID": model}),
                mock.patch.object(sys, "argv", ["/usr/bin/adk", "run", "."]),
            ):
                self.assertEqual(get_model_id(), CLI_FALLBACK_MODEL)

    def test_adk_run_keeps_a_model_that_speaks_generate_content(self):
        with (
            mock.patch.dict(os.environ, {"MODEL_ID": "gemini-2.5-flash"}),
            mock.patch.object(sys, "argv", ["/usr/bin/adk", "run", "."]),
        ):
            self.assertEqual(get_model_id(), "gemini-2.5-flash")


class TestCapabilityMatrix(unittest.TestCase):
    """The EAP feature matrix, as assertions."""

    def test_eap_membership(self):
        self.assertTrue(is_eap_model(WALKIE_TALKIE))
        self.assertTrue(is_eap_model("clever-chatter"))
        self.assertFalse(is_eap_model(PRE_EAP))

    def test_only_clever_chatter_thinks(self):
        self.assertTrue(supports_thinking(CLEVER_CHATTER))
        self.assertFalse(supports_thinking(WALKIE_TALKIE))
        self.assertFalse(supports_thinking(PRE_EAP))

    def test_clever_chatter_rejects_blocking_function_calls(self):
        self.assertFalse(supports_blocking_function_calls(CLEVER_CHATTER))
        self.assertTrue(supports_blocking_function_calls(WALKIE_TALKIE))
        self.assertTrue(supports_blocking_function_calls(PRE_EAP))


class TestModelConstruction(unittest.TestCase):
    def test_eap_ids_become_instances(self):
        """LLMRegistry only resolves `gemini-*`, so a string id raises here."""
        model = build_live_model(WALKIE_TALKIE)
        self.assertIsInstance(model, EapLiveGemini)
        self.assertEqual(model.model, WALKIE_TALKIE)

    def test_non_eap_ids_stay_strings(self):
        """Unchanged path: ADK resolves the name exactly as it did before."""
        self.assertEqual(build_live_model(PRE_EAP), PRE_EAP)

    def test_eap_wrapper_forces_the_gemini_3_x_live_protocol(self):
        """Without this, tool calls are buffered until turn_complete.

        These models call tools asynchronously, so a call can arrive *after*
        turnComplete -- buffering it there means it lands a turn late or never.
        """
        self.assertTrue(issubclass(EapLiveGemini, Gemini))

        captured = {}

        class FakeConnection:
            _is_gemini_3_x_live = False

        import contextlib

        @contextlib.asynccontextmanager
        async def fake_connect(self, llm_request):
            yield FakeConnection()

        async def run():
            with mock.patch.object(Gemini, "connect", fake_connect):
                async with EapLiveGemini(model=WALKIE_TALKIE).connect(None) as conn:
                    captured["flag"] = conn._is_gemini_3_x_live

        import asyncio

        asyncio.run(run())
        self.assertTrue(captured["flag"])

    def test_thinking_config_is_clever_chatter_only(self):
        self.assertIsNone(build_generate_content_config(WALKIE_TALKIE))
        self.assertIsNone(build_generate_content_config(PRE_EAP))

        config = build_generate_content_config(CLEVER_CHATTER)
        self.assertEqual(
            config.thinking_config.thinking_level, types.ThinkingLevel.MINIMAL
        )

    def test_thinking_level_is_tunable(self):
        with mock.patch.dict(os.environ, {"THINKING_LEVEL": "high"}):
            config = build_generate_content_config(CLEVER_CHATTER)
        self.assertEqual(
            config.thinking_config.thinking_level, types.ThinkingLevel.HIGH
        )


if __name__ == "__main__":
    unittest.main()
