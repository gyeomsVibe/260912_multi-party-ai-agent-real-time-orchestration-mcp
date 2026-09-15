"""
harness/ollama_worker.py
Local Slave Harness Worker - Trinity-ACE Protocol

Capabilities:
1. AST Skeleton Extraction: Parses Python source and replaces implementation bodies with `...`
   preserving signatures, type annotations, and docstrings (80-90% token reduction).
2. RTK (Reasoning-Trace Kernel) Error Vector Extractor: Compresses massive tracebacks into
   an actionable 3-line error summary (target file, line number, exception type).
3. Local Ollama Client (qwen2.5-coder:3b): 15-second fail-fast timeout with seamless fallback
   to Antigravity / Gemini Flash cloud escalation route.
"""

import ast
import json
import re
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List


class ASTSkeletonTransformer(ast.NodeTransformer):
    """
    Replaces function and method bodies with Ellipsis (...) or pass,
    while retaining docstrings, decorators, argument types, and return annotations.
    """

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._strip_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._strip_body(node)

    def _strip_body(self, node):
        self.generic_visit(node)
        docstring = ast.get_docstring(node)
        new_body = []
        if docstring:
            new_body.append(ast.Expr(value=ast.Constant(value=docstring)))
        new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
        node.body = new_body
        return node


class OllamaWorker:
    """Local preprocessing and slave task worker."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:3b",
        timeout_seconds: float = 15.0
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def check_health(self) -> bool:
        """Checks if local Ollama daemon is reachable and responding."""
        url = f"{self.base_url}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def extract_ast_skeleton(source_code: str) -> Dict[str, Any]:
        """
        Parse Python source and return an AST skeleton with empty function bodies.

        docs/16 correction O-1: this used to `return source_code` on any parse failure,
        which silently turned a 95% token reduction into a 0% reduction and pushed the
        full raw source into the commander's prompt - the exact Zero Code Pollution
        violation docs/10 forbids. A failure is now reported, never disguised.

        Returns a dict with 'status' ('OK' | 'PARSE_FAILED'), 'skeleton', and metrics.
        """
        original_chars = len(source_code)
        try:
            tree = ast.parse(source_code)
            transformer = ASTSkeletonTransformer()
            transformed_tree = transformer.visit(tree)
            ast.fix_missing_locations(transformed_tree)
            skeleton = ast.unparse(transformed_tree)
            return {
                "status": "OK",
                "skeleton": skeleton,
                "original_chars": original_chars,
                "skeleton_chars": len(skeleton),
                "reduction_pct": round(100.0 * (1 - len(skeleton) / original_chars), 1)
                if original_chars else 0.0,
            }
        except SyntaxError as exc:
            return {
                "status": "PARSE_FAILED",
                "skeleton": "",
                "error": f"{type(exc).__name__}: {exc}",
                "original_chars": original_chars,
                "skeleton_chars": 0,
                "reduction_pct": 0.0,
                "instruction": (
                    "Source could not be parsed. DO NOT forward the raw source to a "
                    "cloud agent; fix the syntax error locally or send only the "
                    "RTK error vector."
                ),
            }

    @staticmethod
    def extract_rtk_error_vector(traceback_text: str) -> Dict[str, Any]:
        """
        Extracts the essential 3-5 line failure vector from verbose execution logs.
        Reduces prompt tokens by 95% compared to raw full tracebacks.
        """
        lines = [line.rstrip() for line in traceback_text.splitlines() if line.strip()]
        if not lines:
            return {
                "file": None,
                "line": None,
                "failing_symbol": None,
                "exception_type": "Unknown",
                "message": "Empty error text",
                "compact_summary": "Empty error text"
            }

        file_match = None
        line_num = None
        symbol = None
        exception_type = "RuntimeError"
        exception_msg = lines[-1]

        # Scan for last 'File "...", line X, in Y' pattern
        for line in reversed(lines):
            m = re.search(r'File "([^"]+)", line (\d+)(?:, in (\w+))?', line)
            if m:
                file_match = m.group(1)
                line_num = int(m.group(2))
                symbol = m.group(3) or "module"
                break

        # Check last line for exception name: message.
        # O-1/O-3: the pattern must accept dotted, qualified names - `sqlite3.OperationalError`,
        # `urllib.error.URLError` - which are the common case in real tracebacks and which
        # the original unqualified pattern silently misreported as "RuntimeError".
        m_exc = re.match(
            r"^((?:[a-zA-Z_]\w*\.)*[a-zA-Z_]\w*(?:Error|Exception|Warning|Interrupt|Exit)):?\s*(.*)$",
            lines[-1],
        )
        if m_exc:
            exception_type = m_exc.group(1)
            exception_msg = m_exc.group(2)

        summary = f"[{exception_type}] in {file_match or 'unknown'}:{line_num or '?'} ({symbol or '?'}): {exception_msg}"

        return {
            "file": file_match,
            "line": line_num,
            "failing_symbol": symbol,
            "exception_type": exception_type,
            "message": exception_msg,
            "compact_summary": summary
        }

    def generate(self, prompt: str, system: Optional[str] = None, mock_response: Optional[str] = None) -> Dict[str, Any]:
        """
        Sends a generation request to local Ollama.
        If timeout exceeds 15.0 seconds or connection fails, immediately triggers
        cloud escalation fallback.
        """
        if mock_response is not None:
            return {
                "status": "SUCCESS",
                "source": "OLLAMA_MOCK",
                "model": self.model,
                "response": mock_response,
                "duration_ms": 12.0
            }

        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False
        }
        if system:
            payload["system"] = system

        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )

        start_time = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                duration_ms = (time.time() - start_time) * 1000.0
                return {
                    "status": "SUCCESS",
                    "source": "OLLAMA_LOCAL",
                    "model": self.model,
                    "response": data.get("response", ""),
                    "duration_ms": duration_ms
                }
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            # 15-second Fail-Fast Cloud Fallback Trigger
            return {
                "status": "FAILFAST_FALLBACK_CLOUD",
                "source": "ESCALATED_ANTIGRAVITY_GEMINI_FLASH",
                "error": str(exc),
                "duration_ms": duration_ms,
                "instruction": "Local slave unavailable or timed out (>15s). Route to Gemini Flash cloud engine."
            }


if __name__ == "__main__":
    sample_code = '''
def calculate_metrics(data: list[float], scale: float = 1.0) -> dict:
    """Calculate mean and variance of the data series."""
    total = sum(data) * scale
    avg = total / len(data)
    variance = sum((x - avg) ** 2 for x in data) / len(data)
    return {"mean": avg, "variance": variance}
'''
    print("--- AST SKELETON ---")
    print(OllamaWorker.extract_ast_skeleton(sample_code)["skeleton"])

    sample_tb = '''
Traceback (most recent call last):
  File "core/calculator.py", line 42, in calculate_metrics
    avg = total / len(data)
ZeroDivisionError: division by zero
'''
    print("\n--- RTK ERROR VECTOR ---")
    print(OllamaWorker.extract_rtk_error_vector(sample_tb))
