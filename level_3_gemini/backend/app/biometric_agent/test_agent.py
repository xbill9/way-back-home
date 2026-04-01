import unittest
from biometric_agent.agent import (
    report_digit,
    get_model_id,
    trigger_system_error,
    trigger_heavy_metal_mode,
)
import os
import sys


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
        self.assertEqual(model_id, "gemini-3.1-flash-live-preview")

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
