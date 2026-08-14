import os
import sys
import unittest

from biometric_agent.agent import (
    _last_report,
    get_model_id,
    report_digit,
    trigger_heavy_metal_mode,
    trigger_system_error,
)


class TestBiometricAgent(unittest.TestCase):
    def setUp(self):
        # report_digit's repeat window is module state, so tests would otherwise
        # see each other's calls -- and the second test to report a 3 would get
        # the duplicate answer instead of the one it asserts on.
        _last_report.update(count=None, at=0.0)

    def test_report_digit(self):
        """Test that report_digit returns the correct structure."""
        result = report_digit(3)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 3)

    def test_repeating_a_digit_tells_the_model_to_stop(self):
        """The tool result is the only channel that can interrupt a repeat run.

        The instruction is read once per session; the result is read every time,
        right where the model is deciding whether to call again. A second call
        with the same digit therefore gets a different answer -- and one that
        names the thing to do instead, since "stop" alone leaves the scan with
        no confirmation spoken.
        """
        report_digit(4)
        result = report_digit(4)

        self.assertEqual(result["status"], "already_reported")
        self.assertEqual(result["count"], 4)
        self.assertIn("STOP calling report_digit", result["message"])
        self.assertIn("confirmation", result["message"])

    def test_a_different_digit_is_never_a_repeat(self):
        """Rule 5 of the instruction: every scan is independent.

        A hand that changes between scans must report both counts, so only the
        same digit twice in the window is a repeat.
        """
        report_digit(2)
        self.assertEqual(report_digit(5)["status"], "success")
        self.assertEqual(report_digit(2)["status"], "success")

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
