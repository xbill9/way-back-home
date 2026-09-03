import os
import sys
import unittest

from google.genai import types

from biometric_agent.agent import (
    _build_tools,
    get_model_id,
    report_digit,
    trigger_heavy_metal_mode,
    trigger_system_error,
)


class TestBiometricAgent(unittest.TestCase):
    def test_report_digit(self):
        """Test that report_digit returns the correct structure."""
        result = report_digit(3)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 3)

    def test_trigger_system_error(self):
        """Test that trigger_system_error returns the correct error structure."""
        result = trigger_system_error()
        self.assertEqual(result["status"], "error")
        self.assertIn("offensive input", result["message"])

    def test_trigger_heavy_metal_mode(self):
        """Test that trigger_heavy_metal_mode returns the correct success structure."""
        result = trigger_heavy_metal_mode()
        self.assertEqual(result["status"], "success")
        self.assertIn("Heavy metal protocol", result["message"])

    def test_eap_models_get_silent_tool_results(self):
        """EAP models must not let the tool result prompt a turn.

        Traced at the wire on 2026-08-13: walkie-talkie ends its turn 4ms after
        a `report_digit` call, having said nothing (turn_complete arrives before
        our response, 38 times out of 38). Our response is then what starts the
        next turn -- median 628ms later -- in which it re-reads the same video
        and calls again. Runs of 13-16 calls follow, and the model never speaks,
        so the confirmation is lost entirely.

        SILENT means "add the result to context, do not trigger generation", so
        the loop has nothing to run on. Measured: exactly 1 call per scan across
        10 scans, 10/10 spoken, 10/10 correct.

        This only works paired with main.py asking for the confirmation on the
        first call (STORM_NUDGE_AFTER defaults to 1 when this is SILENT). SILENT
        on its own is a mute scanner -- which is exactly how it shipped once.
        """
        for model in ("models/walkie-talkie", "models/clever-chatter"):
            for tool in _build_tools(model):
                self.assertEqual(
                    tool.response_scheduling,
                    types.FunctionResponseScheduling.SILENT,
                    f"{model} / {tool.name}",
                )

    def test_non_eap_models_get_no_scheduling(self):
        """Everything else keeps BLOCKING, which is what level_3_new sends.

        gemini-3.1-flash-live-preview is BLOCKING-only -- NON_BLOCKING
        declarations are unsupported -- and it does not need help: measured 1
        call per scan across 10 scans with no intervention at all.
        """
        for model in ("gemini-3.1-flash-live-preview", "gemini-2.5-flash"):
            for tool in _build_tools(model):
                self.assertIsNone(tool.response_scheduling, f"{model} / {tool.name}")

    def test_get_model_id_default(self):
        """Test that get_model_id returns the default model when no env is set."""
        # Ensure MODEL_ID env var is not set for this test
        original_model = os.environ.get("MODEL_ID")
        if "MODEL_ID" in os.environ:
            del os.environ["MODEL_ID"]

        # We need to mock sys.argv to not include 'adk run'
        original_argv = sys.argv
        sys.argv = ["test_agent.py"]

        model_id = get_model_id()
        self.assertEqual(model_id, "models/walkie-talkie")

        sys.argv = original_argv
        if original_model:
            os.environ["MODEL_ID"] = original_model

    def test_get_model_id_env(self):
        """Test that get_model_id respects the MODEL_ID environment variable."""
        original_model = os.environ.get("MODEL_ID")
        os.environ["MODEL_ID"] = "test-model"
        model_id = get_model_id()
        self.assertEqual(model_id, "test-model")

        if original_model:
            os.environ["MODEL_ID"] = original_model
        else:
            del os.environ["MODEL_ID"]


if __name__ == "__main__":
    unittest.main()
