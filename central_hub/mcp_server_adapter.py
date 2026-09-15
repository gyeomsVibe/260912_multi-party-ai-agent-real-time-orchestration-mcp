"""
central_hub/mcp_server_adapter.py
Official MCP Stdio Server Adapter - Trinity-ACE Protocol

Capabilities:
1. Standard JSON-RPC 2.0 Stdio Transport compatible with Claude Code (.mcp.json) and Codex CLI.
2. Memory Pointer Vault: Automatically converts payloads > 1KB into compact `ref://vault/<sha256>`
   pointers (99% token reduction based on 2026 arXiv:2511.22729).
3. Idempotency Key Ledger: Prevents duplicate execution of effectful tasks during network retries.
4. Exposes 6 Core Trinity-ACE Tools:
   - trinity_send_card
   - trinity_read_inbox
   - trinity_acquire_lock
   - trinity_release_lock
   - trinity_store_artifact
   - trinity_triage_error
"""

import sys
import os
import json
import hashlib
import time
import re
from typing import Dict, Any, List, Optional
from central_hub.hub_daemon import SingleWriterDB, NON_DELIVERABLE_EVENT_TYPES
from central_hub.triage_classifier import TriageClassifier

#: C-2: `artifact_type` reaches this module as a model-authored tool argument. Anything
#: other than a bare alphanumeric extension is a path-traversal attempt.
_SAFE_ARTIFACT_TYPE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_SAFE_VAULT_FILENAME = re.compile(r"^[A-Za-z0-9]{1,128}\.[A-Za-z0-9]{1,16}$")


class MemoryVault:
    """Stores oversized payloads to disk and returns ultra-compact URI references."""

    def __init__(self, vault_dir: Optional[str] = None):
        if vault_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            vault_dir = os.path.join(base_dir, "vault")
        self.vault_dir = os.path.realpath(vault_dir)
        os.makedirs(self.vault_dir, exist_ok=True)

    def _contained_path(self, filename: str) -> str:
        """Resolve `filename` inside the vault, refusing anything that escapes it."""
        candidate = os.path.realpath(os.path.join(self.vault_dir, filename))
        if os.path.commonpath([self.vault_dir, candidate]) != self.vault_dir:
            raise ValueError(f"Path escapes vault directory: {filename!r}")
        return candidate

    def store(self, content: str, artifact_type: str = "text") -> Dict[str, Any]:
        """Saves content and returns a pointer reference."""
        if not _SAFE_ARTIFACT_TYPE.match(artifact_type or ""):
            raise ValueError(
                f"Invalid artifact_type {artifact_type!r}: expected 1-16 alphanumeric characters"
            )

        content_bytes = content.encode("utf-8")
        sha = hashlib.sha256(content_bytes).hexdigest()
        filename = f"{sha}.{artifact_type}"
        filepath = self._contained_path(filename)

        # Atomic write via temp file
        temp_filepath = f"{filepath}.tmp"
        with open(temp_filepath, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_filepath, filepath)

        return {
            "ref_uri": f"ref://vault/{filename}",
            "sha256": sha,
            "size_bytes": len(content_bytes),
            "pointer_string": f"[POINTER: ref://vault/{filename} | {len(content_bytes)} bytes]"
        }

    def retrieve(self, ref_uri: str) -> Optional[str]:
        """Retrieves content by URI reference. Returns None for anything unsafe."""
        m = re.match(r"^ref://vault/(.+)$", ref_uri or "")
        if not m:
            return None
        filename = m.group(1)
        if not _SAFE_VAULT_FILENAME.match(filename):
            return None
        try:
            filepath = self._contained_path(filename)
        except ValueError:
            return None
        if not os.path.isfile(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()


class MCPServerAdapter:
    """Implements Model Context Protocol (MCP) JSON-RPC 2.0 over Stdio."""

    #: A-2: newest first. `initialize` echoes the client's version when it is supported
    #: instead of unconditionally announcing the legacy 2024-11-05 revision.
    SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
    LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
    PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION  # backwards-compatible alias

    MAX_INLINE_PAYLOAD_BYTES = 1024  # 1KB limit before pointerization
    #: M-5: how long a read card stays invisible before it is redelivered.
    INBOX_LEASE_SECONDS = 300.0
    #: C-1: a lock held longer than this is treated as abandoned by a dead agent.
    LOCK_LEASE_SECONDS = 900.0
    #: T-3: an IN_FLIGHT idempotency reservation older than this is treated as abandoned.
    IDEMPOTENCY_RESERVATION_SECONDS = 120.0

    def __init__(self, db_path: str = "central_hub.db", vault_dir: Optional[str] = None):
        self.db = SingleWriterDB(db_path)
        self.vault = MemoryVault(vault_dir)

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Returns schemas for the 6 core Trinity-ACE tools."""
        return [
            {
                "name": "trinity_send_card",
                "description": "Send a concise 3-line task card to an agent or broadcast to the council with idempotency.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sender": {"type": "string", "description": "Agent ID of sender (e.g. codex, claude, antigravity)"},
                        "target": {"type": "string", "description": "Target agent ID or 'ALL'"},
                        "task_name": {"type": "string", "description": "Short task title"},
                        "card_text": {"type": "string", "description": "Task card text (concise, max 3 lines)"},
                        "payload": {"type": "object", "description": "Optional payload data (auto-pointerized if >1KB)"},
                        "idempotency_key": {"type": "string", "description": "Unique key to prevent duplicate execution"}
                    },
                    "required": ["sender", "target", "task_name", "card_text"]
                }
            },
            {
                "name": "trinity_read_inbox",
                "description": "Read pending task cards for this agent and automatically acknowledge receipt.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "Agent ID to fetch cards for"},
                        "limit": {"type": "integer", "description": "Max cards to read (default 5)"}
                    },
                    "required": ["agent_id"]
                }
            },
            {
                "name": "trinity_ack_card",
                "description": "Confirm a leased task card as completed so it is not redelivered.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "Agent ID confirming the card"},
                        "message_id": {"type": "integer", "description": "Message ID from trinity_read_inbox"}
                    },
                    "required": ["agent_id", "message_id"]
                }
            },
            {
                "name": "trinity_acquire_lock",
                "description": (
                    "Acquire an atomic exclusive lock on a file path or shared resource. "
                    "Locks expire after 900s; call again with the same holder_id to renew "
                    "the lease during long tasks, or another agent may take it over."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_path": {"type": "string", "description": "Relative file path or resource key"},
                        "holder_id": {"type": "string", "description": "Agent ID claiming the lock"}
                    },
                    "required": ["resource_path", "holder_id"]
                }
            },
            {
                "name": "trinity_release_lock",
                "description": "Release a previously acquired exclusive resource lock.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_path": {"type": "string", "description": "Relative file path or resource key"},
                        "holder_id": {"type": "string", "description": "Agent ID releasing the lock"}
                    },
                    "required": ["resource_path", "holder_id"]
                }
            },
            {
                "name": "trinity_store_artifact",
                "description": "Store large diffs or logs to disk and get a lightweight ref:// pointer (Memory Pointer Pattern).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Large text content to store"},
                        "artifact_type": {"type": "string", "description": "File extension / type (diff, log, json)"}
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "trinity_triage_error",
                "description": "Classify a terminal error log before modifying code to prevent wasteful debug loops.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "error_log": {"type": "string", "description": "Terminal stderr or failure traceback"},
                        "exit_code": {"type": "integer", "description": "Process exit code (default 1)"}
                    },
                    "required": ["error_log"]
                }
            }
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches tool calls to respective handlers."""
        if name == "trinity_send_card":
            return self._handle_send_card(arguments)
        elif name == "trinity_read_inbox":
            return self._handle_read_inbox(arguments)
        elif name == "trinity_ack_card":
            return self._handle_ack_card(arguments)
        elif name == "trinity_acquire_lock":
            return self._handle_acquire_lock(arguments)
        elif name == "trinity_release_lock":
            return self._handle_release_lock(arguments)
        elif name == "trinity_store_artifact":
            return self._handle_store_artifact(arguments)
        elif name == "trinity_triage_error":
            return self._handle_triage_error(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")

    def _handle_send_card(self, args: Dict[str, Any]) -> Dict[str, Any]:
        idem_key = args.get("idempotency_key")
        if idem_key:
            # M-6: reserve the key FIRST. The original code inserted the card and only
            # then wrote the ledger, so concurrent callers with the same key each saw an
            # empty ledger and each published a card.
            reserve = self.db.execute_write(
                "INSERT OR IGNORE INTO idempotency_ledger (idempotency_key, result_json, created_at)"
                " VALUES (?, ?, ?)",
                (idem_key, json.dumps({"status": "IN_FLIGHT"}), time.time())
            )
            if reserve.get("status") != "OK":
                return {"status": "ERROR", "error": reserve.get("error", "ledger write failed")}
            if reserve.get("rowcount", 0) == 0:
                # T-3: a caller that crashed between reserve and publish would leave the
                # key IN_FLIGHT forever and swallow every retry. Reclaim stale reservations.
                reclaim = self.db.execute_write(
                    "UPDATE idempotency_ledger SET created_at = ?"
                    " WHERE idempotency_key = ? AND result_json = ? AND created_at < ?",
                    (time.time(), idem_key, json.dumps({"status": "IN_FLIGHT"}),
                     time.time() - self.IDEMPOTENCY_RESERVATION_SECONDS)
                )
                if reclaim.get("rowcount", 0) != 1:
                    existing = self.db.execute_read(
                        "SELECT result_json FROM idempotency_ledger WHERE idempotency_key = ?",
                        (idem_key,)
                    )
                    return json.loads(existing[0][0]) if existing else {"status": "DUPLICATE"}

        sender = args["sender"]
        target = args["target"]
        task_name = args["task_name"]
        card_text = args["card_text"]
        payload = args.get("payload", {})

        # Automatic Memory Pointer transformation for large payloads (>1KB)
        payload_str = json.dumps(payload, ensure_ascii=False)
        if len(payload_str.encode("utf-8")) > self.MAX_INLINE_PAYLOAD_BYTES:
            vault_res = self.vault.store(payload_str, artifact_type="json")
            payload = {
                "_pointer_active": True,
                "ref_uri": vault_res["ref_uri"],
                "size_bytes": vault_res["size_bytes"],
                "summary": f"Stored in Memory Vault ({vault_res['size_bytes']} bytes). Retrieve with ref_uri."
            }

        full_card = {
            "task_name": task_name,
            "card_text": card_text,
            "payload": payload
        }

        res = self.db.execute_write(
            "INSERT INTO message_bus (sender_id, target_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (sender, target, "TASK_CARD", json.dumps(full_card, ensure_ascii=False), time.time())
        )

        output = {
            "status": "SENT",
            "message_id": res.get("lastrowid"),
            "sender": sender,
            "target": target,
            "is_pointer": payload.get("_pointer_active", False)
        }

        if idem_key:
            if res.get("status") == "OK":
                self.db.execute_write(
                    "UPDATE idempotency_ledger SET result_json = ? WHERE idempotency_key = ?",
                    (json.dumps(output, ensure_ascii=False), idem_key)
                )
            else:
                # Release the reservation so a retry is not permanently swallowed.
                self.db.execute_write(
                    "DELETE FROM idempotency_ledger WHERE idempotency_key = ?", (idem_key,)
                )

        return output

    def _handle_read_inbox(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lease pending cards for an agent (M-5).

        The original implementation acknowledged on read, so a card was destroyed the
        moment it was handed out - an agent that crashed mid-task lost the work with no
        redelivery. Reading now takes a time-boxed lease; only an explicit
        `trinity_ack_card` confirms completion. An expired lease is redelivered.
        Infrastructure markers such as the keep-alive probe are never delivered (A-1).
        """
        agent_id = args["agent_id"]
        limit = int(args.get("limit", 5))
        now = time.time()

        placeholders = ",".join("?" for _ in NON_DELIVERABLE_EVENT_TYPES)
        rows = self.db.execute_read(
            f"""
            SELECT m.message_id, m.sender_id, m.target_id, m.event_type, m.payload, m.created_at
            FROM message_bus m
            LEFT JOIN inbox_acks a ON m.message_id = a.message_id AND a.agent_id = ?
            WHERE (m.target_id = ? OR m.target_id = 'ALL')
              AND m.event_type NOT IN ({placeholders})
              AND (a.message_id IS NULL OR (a.confirmed = 0 AND a.leased_until < ?))
            ORDER BY m.message_id ASC
            LIMIT ?
            """,
            (agent_id, agent_id, *NON_DELIVERABLE_EVENT_TYPES, now, limit)
        )

        cards = []
        lease_until = now + self.INBOX_LEASE_SECONDS
        for r in rows:
            mid = r[0]
            lease = self.db.execute_write(
                """
                INSERT INTO inbox_acks (message_id, agent_id, acked_at, leased_until, confirmed)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(message_id, agent_id) DO UPDATE SET
                    acked_at = excluded.acked_at,
                    leased_until = excluded.leased_until
                WHERE inbox_acks.confirmed = 0 AND inbox_acks.leased_until < ?
                """,
                (mid, agent_id, now, lease_until, now)
            )
            if lease.get("status") != "OK" or lease.get("rowcount", 0) != 1:
                continue  # another reader won the lease; do not hand out a duplicate
            cards.append({
                "message_id": mid,
                "sender_id": r[1],
                "target_id": r[2],
                "event_type": r[3],
                "content": json.loads(r[4]),
                "created_at": r[5],
                "lease_expires_at": lease_until,
            })

        return {"agent_id": agent_id, "count": len(cards), "cards": cards}

    def _handle_ack_card(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Confirm a leased card so it is never redelivered (M-5)."""
        agent_id = args["agent_id"]
        message_id = int(args["message_id"])
        res = self.db.execute_write(
            "UPDATE inbox_acks SET confirmed = 1, acked_at = ? WHERE message_id = ? AND agent_id = ?",
            (time.time(), message_id, agent_id)
        )
        if res.get("status") != "OK":
            return {"status": "ERROR", "error": res.get("error", "write failed")}
        if res.get("rowcount", 0) == 0:
            return {"status": "NOT_LEASED", "message_id": message_id, "agent_id": agent_id}
        return {"status": "ACKED", "message_id": message_id, "agent_id": agent_id}

    def _handle_acquire_lock(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Acquire an exclusive lock atomically (C-1).

        The original implementation read the table, then upserted with DO UPDATE. That is
        a check-then-act race - under contention every caller read "free" and every caller
        was told ACQUIRED - and DO UPDATE also let any agent steal a live lock. A single
        conditional statement now decides the winner, and only the writer thread executes
        it, so the serialized queue provides the mutual exclusion.
        """
        path = args["resource_path"]
        holder = args["holder_id"]
        now = time.time()
        stale_before = now - self.LOCK_LEASE_SECONDS

        # Advisory only: used to label the outcome. The write below decides the winner.
        prior = self.db.execute_read(
            "SELECT holder_id, acquired_at FROM resource_locks WHERE resource_path = ?", (path,)
        )
        prior_holder = prior[0][0] if prior else None
        prior_acquired = prior[0][1] if prior else None

        res = self.db.execute_write(
            """
            INSERT INTO resource_locks (resource_path, holder_id, acquired_at)
            VALUES (?, ?, ?)
            ON CONFLICT(resource_path) DO UPDATE SET
                holder_id = excluded.holder_id,
                acquired_at = excluded.acquired_at
            WHERE resource_locks.holder_id = excluded.holder_id
               OR resource_locks.acquired_at < ?
            """,
            (path, holder, now, stale_before)
        )

        if res.get("status") != "OK":
            return {"status": "ERROR", "resource_path": path, "error": res.get("error", "write failed")}

        if res.get("rowcount", 0) == 1:
            return {
                "status": "ACQUIRED",
                "resource_path": path,
                "holder_id": holder,
                "lease_expires_at": now + self.LOCK_LEASE_SECONDS,
                "reentrant": prior_holder == holder,
                "stale_takeover": prior_holder is not None
                and prior_holder != holder
                and prior_acquired is not None
                and prior_acquired < stale_before,
            }

        rows = self.db.execute_read(
            "SELECT holder_id, acquired_at FROM resource_locks WHERE resource_path = ?", (path,)
        )
        current_holder, acquired = rows[0] if rows else ("unknown", 0.0)
        return {
            "status": "LOCKED",
            "resource_path": path,
            "current_holder": current_holder,
            "acquired_at": acquired,
            "error": f"Resource locked by {current_holder}"
        }

    def _handle_release_lock(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["resource_path"]
        holder = args["holder_id"]

        rows = self.db.execute_read(
            "SELECT holder_id FROM resource_locks WHERE resource_path = ?",
            (path,)
        )
        if not rows:
            return {"status": "NOT_LOCKED", "resource_path": path}

        if rows[0][0] != holder:
            return {"status": "FORBIDDEN", "error": f"Lock owned by {rows[0][0]}, cannot be released by {holder}"}

        self.db.execute_write("DELETE FROM resource_locks WHERE resource_path = ?", (path,))
        return {"status": "RELEASED", "resource_path": path}

    def _handle_store_artifact(self, args: Dict[str, Any]) -> Dict[str, Any]:
        content = args["content"]
        art_type = args.get("artifact_type", "txt")
        return self.vault.store(content, artifact_type=art_type)

    def _handle_triage_error(self, args: Dict[str, Any]) -> Dict[str, Any]:
        error_log = args["error_log"]
        exit_code = int(args.get("exit_code", 1))
        return TriageClassifier.classify(error_log, exit_code=exit_code)

    def handle_json_rpc(self, request_str: str) -> Optional[str]:
        """Processes a single JSON-RPC 2.0 message."""
        try:
            req = json.loads(request_str)
        except Exception as e:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
            })

        # A-2: JSON-RPC batching was removed from MCP in revision 2025-06-18.
        if isinstance(req, list):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "JSON-RPC batching is not supported (removed in MCP 2025-06-18)"
                }
            })
        if not isinstance(req, dict):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"}
            })

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        # Handle notifications (no id)
        if req_id is None:
            if method == "notifications/initialized":
                pass
            return None

        if method == "initialize":
            # A-2: honour the client's requested revision when we support it.
            requested = params.get("protocolVersion")
            negotiated = (
                requested if requested in self.SUPPORTED_PROTOCOL_VERSIONS
                else self.LATEST_PROTOCOL_VERSION
            )
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "trinity-mcp-hub",
                        "version": "1.0.0"
                    }
                }
            })
        elif method == "ping":
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_definitions()}
            })
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                res = self.execute_tool(tool_name, tool_args)
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}
                        ]
                    }
                })
            except Exception as exc:
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)}
                })
        else:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            })

    def run_stdio(self):
        """Runs standard input/output loop for JSON-RPC 2.0 communication."""
        # M-7: on a Korean Windows host the default console codec is cp949, which
        # corrupts non-ASCII JSON-RPC frames, and the default newline translation
        # rewrites the '\n' that delimits them.
        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8", newline="\n")
            except (AttributeError, ValueError):
                pass

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            resp = self.handle_json_rpc(line)
            if resp:
                sys.stdout.write(resp + "\n")
                sys.stdout.flush()

    def close(self):
        self.db.close()


if __name__ == "__main__":
    adapter = MCPServerAdapter()
    try:
        adapter.run_stdio()
    finally:
        adapter.close()
