"""
tests/test_ollama_worker.py
Unit tests for OllamaWorker, AST skeleton generation, and RTK error vector compression.
"""

import unittest
from harness.ollama_worker import OllamaWorker


class TestOllamaWorker(unittest.TestCase):
    def test_extract_ast_skeleton(self):
        source = '''
class PipelineManager:
    """Manages multi-stage compilation pipeline."""

    def __init__(self, name: str):
        self.name = name

    def execute_stage(self, stage_id: int, payload: dict) -> bool:
        """Executes a single stage in isolation."""
        res = stage_id > 0
        if not res:
            raise ValueError("Stage ID must be positive")
        return res
'''
        result = OllamaWorker.extract_ast_skeleton(source)
        self.assertEqual(result["status"], "OK")
        skeleton = result["skeleton"]
        # Check that docstring and signatures remain, but bodies are stripped to Ellipsis (...)
        self.assertIn("class PipelineManager:", skeleton)
        self.assertIn("def execute_stage(self, stage_id: int, payload: dict) -> bool:", skeleton)
        self.assertIn('"""Executes a single stage in isolation."""', skeleton)
        self.assertNotIn("raise ValueError", skeleton)
        self.assertNotIn("res = stage_id > 0", skeleton)
        self.assertIn("...", skeleton)

    def test_extract_rtk_error_vector(self):
        traceback_text = '''
Traceback (most recent call last):
  File "C:\\runtime\\server.py", line 128, in handle_request
    response = handler.process(data)
  File "C:\\runtime\\handler.py", line 45, in process
    return self.validator.validate(data)
  File "C:\\runtime\\validator.py", line 19, in validate
    raise KeyError("Missing required field 'client_token'")
KeyError: "Missing required field 'client_token'"
'''
        rtk = OllamaWorker.extract_rtk_error_vector(traceback_text)
        self.assertEqual(rtk["line"], 19)
        self.assertEqual(rtk["failing_symbol"], "validate")
        self.assertEqual(rtk["exception_type"], "KeyError")
        self.assertIn("client_token", rtk["message"])
        self.assertIn("[KeyError] in C:\\runtime\\validator.py:19 (validate)", rtk["compact_summary"])

    def test_mock_generation(self):
        worker = OllamaWorker()
        res = worker.generate("Generate docstring", mock_response="Generated Docstring")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["source"], "OLLAMA_MOCK")
        self.assertEqual(res["response"], "Generated Docstring")

    def test_failfast_cloud_fallback(self):
        # Point to an unreachable port to verify 15s failfast / immediate fallback behavior
        worker = OllamaWorker(base_url="http://127.0.0.1:59999", timeout_seconds=1.0)
        res = worker.generate("test prompt")
        self.assertEqual(res["status"], "FAILFAST_FALLBACK_CLOUD")
        self.assertEqual(res["source"], "ESCALATED_ANTIGRAVITY_GEMINI_FLASH")
        self.assertIn("Gemini Flash", res["instruction"])


if __name__ == "__main__":
    unittest.main()
