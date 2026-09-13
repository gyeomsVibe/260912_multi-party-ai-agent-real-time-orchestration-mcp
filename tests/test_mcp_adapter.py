"""
tests/test_mcp_adapter.py
Unit tests for MCPServerAdapter, MemoryVault, Idempotency, and Tool executions.
"""

import unittest
import os
import tempfile
import json
from central_hub.mcp_server_adapter import MCPServerAdapter, MemoryVault


class TestMCPServerAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "mcp_test.db")
        self.vault_dir = os.path.join(self.temp_dir.name, "vault")
        self.adapter = MCPServerAdapter(db_path=self.db_path, vault_dir=self.vault_dir)

    def tearDown(self):
        self.adapter.close()
        self.temp_dir.cleanup()

    def test_json_rpc_initialize_and_ping(self):
        # Test initialize
        req_init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        res_init = json.loads(self.adapter.handle_json_rpc(req_init))
        self.assertEqual(res_init["id"], 1)
        self.assertIn(res_init["result"]["protocolVersion"],
                      MCPServerAdapter.SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual(res_init["result"]["serverInfo"]["name"], "trinity-mcp-hub")

        # Test ping
        req_ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        res_ping = json.loads(self.adapter.handle_json_rpc(req_ping))
        self.assertEqual(res_ping["id"], 2)
        self.assertEqual(res_ping["result"], {})

    def test_tools_list_six_core_tools(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
        res = json.loads(self.adapter.handle_json_rpc(req))
        tools = res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected = [
            "trinity_send_card",
            "trinity_read_inbox",
            "trinity_acquire_lock",
            "trinity_release_lock",
            "trinity_store_artifact",
            "trinity_triage_error"
        ]
        for exp in expected:
            self.assertIn(exp, tool_names)

    def test_send_and_read_card_flow(self):
        # 1. Send card
        send_req = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "trinity_send_card",
                "arguments": {
                    "sender": "codex_brain",
                    "target": "claude_immune",
                    "task_name": "patch_ast_parser",
                    "card_text": "Refactor AST parser for faster visitor traversal."
                }
            }
        }
        res_send = json.loads(self.adapter.handle_json_rpc(json.dumps(send_req)))
        card_content = json.loads(res_send["result"]["content"][0]["text"])
        self.assertEqual(card_content["status"], "SENT")
        self.assertEqual(card_content["target"], "claude_immune")

        # 2. Read inbox for claude_immune
        read_req = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "trinity_read_inbox",
                "arguments": {"agent_id": "claude_immune", "limit": 5}
            }
        }
        res_read = json.loads(self.adapter.handle_json_rpc(json.dumps(read_req)))
        read_content = json.loads(res_read["result"]["content"][0]["text"])
        self.assertEqual(read_content["count"], 1)
        self.assertEqual(read_content["cards"][0]["content"]["task_name"], "patch_ast_parser")

        # 3. Subsequent read should be empty because of automatic ACK
        res_read2 = json.loads(self.adapter.handle_json_rpc(json.dumps(read_req)))
        read_content2 = json.loads(res_read2["result"]["content"][0]["text"])
        self.assertEqual(read_content2["count"], 0)

    def test_memory_pointer_automatic_vaulting(self):
        # Create oversized payload (> 1KB)
        large_text = "A" * 5000
        send_req = {
            "jsonrpc": "2.0",
            "id": 200,
            "method": "tools/call",
            "params": {
                "name": "trinity_send_card",
                "arguments": {
                    "sender": "antigravity",
                    "target": "codex",
                    "task_name": "large_diff_dump",
                    "card_text": "Attached large diff for review.",
                    "payload": {"diff_dump": large_text}
                }
            }
        }
        res_send = json.loads(self.adapter.handle_json_rpc(json.dumps(send_req)))
        card_content = json.loads(res_send["result"]["content"][0]["text"])
        self.assertTrue(card_content["is_pointer"])

        # Read card and verify pointer
        read_req = {
            "jsonrpc": "2.0",
            "id": 201,
            "method": "tools/call",
            "params": {
                "name": "trinity_read_inbox",
                "arguments": {"agent_id": "codex"}
            }
        }
        res_read = json.loads(self.adapter.handle_json_rpc(json.dumps(read_req)))
        read_content = json.loads(res_read["result"]["content"][0]["text"])
        received_payload = read_content["cards"][0]["content"]["payload"]
        self.assertTrue(received_payload["_pointer_active"])
        ref_uri = received_payload["ref_uri"]

        # Retrieve content from vault
        retrieved = self.adapter.vault.retrieve(ref_uri)
        self.assertIsNotNone(retrieved)
        retrieved_data = json.loads(retrieved)
        self.assertEqual(retrieved_data["diff_dump"], large_text)

    def test_idempotency_key_duplicate_prevention(self):
        idem_key = "unique_tx_123456"
        send_req = {
            "jsonrpc": "2.0",
            "id": 300,
            "method": "tools/call",
            "params": {
                "name": "trinity_send_card",
                "arguments": {
                    "sender": "codex",
                    "target": "claude",
                    "task_name": "idempotent_task",
                    "card_text": "Do not execute twice.",
                    "idempotency_key": idem_key
                }
            }
        }
        # First send
        res1 = json.loads(self.adapter.handle_json_rpc(json.dumps(send_req)))
        content1 = json.loads(res1["result"]["content"][0]["text"])
        mid1 = content1["message_id"]

        # Duplicate send with same idempotency key
        res2 = json.loads(self.adapter.handle_json_rpc(json.dumps(send_req)))
        content2 = json.loads(res2["result"]["content"][0]["text"])
        mid2 = content2["message_id"]

        self.assertEqual(mid1, mid2)

        # Check total cards in message_bus
        rows = self.adapter.db.execute_read("SELECT count(*) FROM message_bus WHERE sender_id='codex'")
        self.assertEqual(rows[0][0], 1)

    def test_acquire_and_release_lock(self):
        path = "src/core/compiler.py"
        # Claude acquires lock
        res_acq = self.adapter.execute_tool("trinity_acquire_lock", {"resource_path": path, "holder_id": "claude"})
        self.assertEqual(res_acq["status"], "ACQUIRED")

        # Codex attempts to acquire lock -> locked
        res_acq2 = self.adapter.execute_tool("trinity_acquire_lock", {"resource_path": path, "holder_id": "codex"})
        self.assertEqual(res_acq2["status"], "LOCKED")

        # Release lock by claude
        res_rel = self.adapter.execute_tool("trinity_release_lock", {"resource_path": path, "holder_id": "claude"})
        self.assertEqual(res_rel["status"], "RELEASED")

        # Codex can now acquire lock
        res_acq3 = self.adapter.execute_tool("trinity_acquire_lock", {"resource_path": path, "holder_id": "codex"})
        self.assertEqual(res_acq3["status"], "ACQUIRED")

    def test_triage_error_tool(self):
        res = self.adapter.execute_tool("trinity_triage_error", {
            "error_log": "sqlite3.OperationalError: database is locked",
            "exit_code": 1
        })
        self.assertEqual(res["category"], "INFRA_ENVIRONMENT")
        self.assertFalse(res["code_edit_permitted"])


if __name__ == "__main__":
    unittest.main()
