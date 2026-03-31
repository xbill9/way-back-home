import unittest
from biometric_agent.agent import report_digit, get_model_id
import os
import sys

class TestBiometricAgent(unittest.TestCase):
    def test_report_digit(self):
        """Test that report_digit returns the correct structure."""
        result = report_digit(3)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["digit"], 3)

    def test_get_model_id_default(self):
        """Test that get_model_id returns the default model when no env is set."""
        # Ensure MODEL_ID env var is not set for this test
        if "MODEL_ID" in os.environ:
            del os.environ["MODEL_ID"]
        
        # We need to mock sys.argv to not include 'adk run'
        original_argv = sys.argv
        sys.argv = ["test_agent.py"]
        
        model_id = get_model_id()
        self.assertEqual(model_id, "gemini-3.1-flash-live-preview")
        
        sys.argv = original_argv

    def test_get_model_id_env(self):
        """Test that get_model_id respects the MODEL_ID environment variable."""
        os.environ["MODEL_ID"] = "test-model"
        model_id = get_model_id()
        self.assertEqual(model_id, "test-model")
        del os.environ["MODEL_ID"]

if __name__ == "__main__":
    unittest.main()
