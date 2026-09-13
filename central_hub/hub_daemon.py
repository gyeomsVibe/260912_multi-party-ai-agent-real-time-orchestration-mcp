"""
central_hub/hub_daemon.py
Central Orchestration Hub Daemon - Trinity-ACE Protocol

Core Capabilities:
1. Single-Writer Serialized Queue for SQLite WAL (Zero Windows NTFS Sharing Violation).
2. Event Bus for Real-Time Multi-Agent Pub/Sub (JSON-RPC 2.0 compatible).
3. Bearer-token authenticated local HTTP control plane.
4. Zero External Dependencies (Standard Python 3.10+ library).

docs/16 corrections applied:
* C-4  ':memory:' now maps to a shared-cache URI kept alive by a keeper connection,
       so the init/write/read connections all see the same database.
* M-1  Write results always carry an explicit status ('OK' | 'ERROR' | 'TIMEOUT')
       and a rowcount; a failed write never looks like a success.
* M-2  Reads reuse a thread-local connection with WAL/busy_timeout pragmas applied.
* M-3  ThreadingHTTPServer so concurrent agent polling is not serialized.
* M-4  Bearer token required for every state-changing endpoint.
* A-1  The keep-alive probe is DISABLED by default and is explicitly labelled as a
       local-only bus event. It does NOT refresh any remote prompt cache: only a real
       API request re-sending the cached prefix can do that, and that request is billed.
"""

import hmac
import itertools
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List, Optional

# Bus events that are infrastructure noise and must never be delivered as task cards (A-1).
NON_DELIVERABLE_EVENT_TYPES = ("CACHE_KEEP_ALIVE_PROBE",)

_MEMORY_DB_COUNTER = itertools.count()


class SingleWriterDB:
    """Dedicated single-thread worker that owns all write operations to SQLite."""

    READ_TIMEOUT_SECONDS = 10.0
    WRITE_TIMEOUT_SECONDS = 10.0

    def __init__(self, db_path: str = ":memory:"):
        # C-4: ':memory:' would otherwise give every connection its own empty database.
        if db_path == ":memory:":
            self.dsn = f"file:trinity_mem_{next(_MEMORY_DB_COUNTER)}?mode=memory&cache=shared"
            self.uri = True
            self.is_memory = True
        else:
            self.dsn = db_path
            self.uri = False
            self.is_memory = False

        self.db_path = db_path
        self.write_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        # M-2: a bounded pool of read connections instead of thread-locals.
        # Thread-local connections cannot be closed from close() - sqlite3 refuses
        # cross-thread use - so a finished worker thread would keep its file handle open
        # and Windows would raise WinError 32 on cleanup, the very fault this module
        # exists to prevent. Pooled connections are checked out one thread at a time
        # (so check_same_thread=False is safe) and are all closable by the owner.
        self._read_pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue()
        self._read_conns: List[sqlite3.Connection] = []
        self._read_conns_lock = threading.Lock()

        # Keeper connection: a shared-cache in-memory database is destroyed when the
        # last connection closes, so one connection must outlive every other.
        self._keeper = self._connect()
        self._init_db(self._keeper)

        self.worker_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.worker_thread.start()

    def _connect(self, same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.dsn,
            uri=self.uri,
            timeout=self.READ_TIMEOUT_SECONDS,
            check_same_thread=same_thread,
        )
        if not self.is_memory:
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")  # M-2
        return conn

    def _init_db(self, conn: sqlite3.Connection):
        """Initialize SQLite schema."""
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            agent_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            current_retry_count INTEGER DEFAULT 0,
            last_heartbeat REAL NOT NULL,
            current_worktree TEXT
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS message_bus (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS resource_locks (
            resource_path TEXT PRIMARY KEY,
            holder_id TEXT NOT NULL,
            acquired_at REAL NOT NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_ledger (
            idempotency_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)

        # M-5: 'leased_until' turns at-most-once delivery into leased at-least-once.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inbox_acks (
            message_id INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            acked_at REAL NOT NULL,
            leased_until REAL NOT NULL DEFAULT 0,
            confirmed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (message_id, agent_id)
        );
        """)

        # Legacy databases predate the lease columns; add them idempotently.
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(inbox_acks);")}
        if "leased_until" not in existing:
            cursor.execute("ALTER TABLE inbox_acks ADD COLUMN leased_until REAL NOT NULL DEFAULT 0;")
        if "confirmed" not in existing:
            cursor.execute("ALTER TABLE inbox_acks ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0;")

        # M-8: keep inbox lookups from degrading as the bus grows.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_bus_target ON message_bus (target_id, message_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_acks_agent ON inbox_acks (agent_id, message_id);"
        )

        conn.commit()

    def _writer_loop(self):
        """Worker loop that executes all SQL write statements sequentially."""
        conn = self._connect()
        try:
            while not self.stop_event.is_set():
                try:
                    item = self.write_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                query, params, response_event, result_box = item
                try:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    conn.commit()
                    result_box["status"] = "OK"
                    result_box["lastrowid"] = cursor.lastrowid
                    result_box["rowcount"] = cursor.rowcount
                except Exception as exc:
                    result_box["status"] = "ERROR"
                    result_box["error"] = str(exc)
                    result_box["rowcount"] = 0
                finally:
                    response_event.set()
                    self.write_queue.task_done()
        finally:
            conn.close()

    def execute_write(self, query: str, params: tuple = ()) -> Dict[str, Any]:
        """Submit a write query and synchronously wait for single-thread execution."""
        response_event = threading.Event()
        result_box: Dict[str, Any] = {}
        self.write_queue.put((query, params, response_event, result_box))
        if not response_event.wait(timeout=self.WRITE_TIMEOUT_SECONDS):
            # M-1: never return a bare {} that callers mistake for success.
            return {
                "status": "TIMEOUT",
                "rowcount": 0,
                "error": f"write did not complete within {self.WRITE_TIMEOUT_SECONDS}s",
            }
        result_box.setdefault("rowcount", 0)
        return result_box

    def _checkout_read_conn(self) -> sqlite3.Connection:
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            conn = self._connect(same_thread=False)
            with self._read_conns_lock:
                self._read_conns.append(conn)
            return conn

    def execute_read(self, query: str, params: tuple = ()) -> List[tuple]:
        """Perform concurrent reads via a pooled connection (M-2)."""
        conn = self._checkout_read_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        finally:
            if not self.stop_event.is_set():
                self._read_pool.put(conn)

    def close(self):
        """Shutdown the writer worker thread."""
        self.stop_event.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        errors = []
        with self._read_conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except Exception as exc:  # surfaced, never silently swallowed
                    errors.append(str(exc))
            self._read_conns.clear()
        while not self._read_pool.empty():
            try:
                self._read_pool.get_nowait()
            except queue.Empty:
                break
        self._keeper.close()
        if errors:
            raise RuntimeError(f"failed to close {len(errors)} read connection(s): {errors[0]}")


class CentralHubDaemon:
    """The central orchestrator managing event routing, agent states, and keep-alive."""

    #: Third-party analyses put the break-even for probing an idle cache near ~62 minutes,
    #: and subscription plans (Pro/Plus) pay for probes in session quota rather than cash.
    #: Probing is therefore opt-in, never a 24/7 default (A-1).
    DEFAULT_KEEP_ALIVE_INTERVAL = 270.0

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        db_path: str = ":memory:",
        auth_token: Optional[str] = None,
        keep_alive_enabled: bool = False,
        keep_alive_interval: float = DEFAULT_KEEP_ALIVE_INTERVAL,
    ):
        self.host = host
        self.port = port
        self.db = SingleWriterDB(db_path)
        self.server: Optional[ThreadingHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.keep_alive_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.last_cache_refresh = time.time()
        self.keep_alive_enabled = keep_alive_enabled
        self.keep_alive_interval = keep_alive_interval
        # M-4: an unauthenticated local port is a task-card injection channel.
        self.auth_token = auth_token if auth_token is not None else secrets.token_urlsafe(32)

    # ----- bus -------------------------------------------------------------

    def publish_event(self, sender: str, target: str, event_type: str, payload: dict) -> Dict[str, Any]:
        """Publish an event to the message bus via single-writer queue."""
        payload_str = json.dumps(payload, ensure_ascii=False)
        return self.db.execute_write(
            "INSERT INTO message_bus (sender_id, target_id, event_type, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sender, target, event_type, payload_str, time.time()),
        )

    def get_events(self, since_id: int = 0) -> List[Dict[str, Any]]:
        """Retrieve events since a given message ID."""
        rows = self.db.execute_read(
            "SELECT message_id, sender_id, target_id, event_type, payload, created_at"
            " FROM message_bus WHERE message_id > ? ORDER BY message_id ASC",
            (since_id,),
        )
        return [
            {
                "message_id": r[0],
                "sender_id": r[1],
                "target_id": r[2],
                "event_type": r[3],
                "payload": json.loads(r[4]),
                "created_at": r[5],
            }
            for r in rows
        ]

    def register_heartbeat(self, agent_id: str, status: str = "IDLE", worktree: str = ""):
        """Register or update agent heartbeat."""
        return self.db.execute_write(
            """
            INSERT INTO agent_sessions (agent_id, status, last_heartbeat, current_worktree)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                status=excluded.status,
                last_heartbeat=excluded.last_heartbeat,
                current_worktree=excluded.current_worktree
            """,
            (agent_id, status, time.time(), worktree),
        )

    # ----- keep-alive ------------------------------------------------------

    def _emit_keep_alive_probe(self) -> Dict[str, Any]:
        """
        Emit a LOCAL bus marker only.

        This does not and cannot refresh an Anthropic/Google prompt cache: a remote TTL
        is only reset by a real API request that re-sends the cached prefix, which is
        billed at the cache-read rate (and consumes subscription quota). The payload
        states this explicitly so downstream agents never treat it as cache protection.
        """
        return self.publish_event(
            sender="hub_daemon",
            target="ALL",
            event_type="CACHE_KEEP_ALIVE_PROBE",
            payload={
                "scope": "LOCAL_BUS_MARKER_ONLY",
                "refreshes_remote_cache": False,
                "timestamp": time.time(),
            },
        )

    def _keep_alive_worker(self):
        while not self.stop_event.wait(1.0):
            if time.time() - self.last_cache_refresh >= self.keep_alive_interval:
                self._emit_keep_alive_probe()
                self.last_cache_refresh = time.time()

    # ----- http ------------------------------------------------------------

    def _is_authorized(self, header_value: Optional[str]) -> bool:
        if not header_value or not header_value.startswith("Bearer "):
            return False
        return hmac.compare_digest(header_value[len("Bearer "):], self.auth_token)

    def start(self):
        """Start the HTTP server and background keep-alive worker."""
        daemon_ref = self

        class HubRequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass  # Suppress default noisy console logs

            def _send_json(self, status_code: int, data: dict):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _guard(self) -> bool:
                if daemon_ref._is_authorized(self.headers.get("Authorization")):
                    return True
                # Drain the request body before answering: on a keep-alive HTTP/1.1
                # connection an unread body is parsed as the next request line.
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    self.rfile.read(length)
                self._send_json(401, {"error": "Unauthorized: Bearer token required"})
                return False

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/health":
                    self._send_json(200, {"status": "HEALTHY", "timestamp": time.time()})
                    return
                if not self._guard():
                    return
                if parsed.path == "/api/events":
                    qs = parse_qs(parsed.query)
                    since_id = int(qs.get("since_id", ["0"])[0])
                    self._send_json(200, {"events": daemon_ref.get_events(since_id)})
                elif parsed.path == "/api/state":
                    sessions = daemon_ref.db.execute_read(
                        "SELECT agent_id, status, last_heartbeat, current_worktree FROM agent_sessions"
                    )
                    self._send_json(200, {"sessions": [
                        {"agent_id": s[0], "status": s[1], "last_heartbeat": s[2], "worktree": s[3]}
                        for s in sessions
                    ]})
                else:
                    self._send_json(404, {"error": "Not Found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                if not self._guard():
                    return
                try:
                    data = self._read_json()
                except Exception as exc:
                    self._send_json(400, {"error": f"Malformed JSON: {exc}"})
                    return

                if parsed.path == "/api/events":
                    res = daemon_ref.publish_event(
                        sender=data.get("sender", "anonymous"),
                        target=data.get("target", "ALL"),
                        event_type=data.get("event_type", "UNKNOWN"),
                        payload=data.get("payload", {}),
                    )
                    # M-1: surface write failures instead of answering 200 with junk.
                    self._send_json(200 if res.get("status") == "OK" else 503, res)
                elif parsed.path == "/api/heartbeat":
                    try:
                        res = daemon_ref.register_heartbeat(
                            agent_id=data["agent_id"],
                            status=data.get("status", "IDLE"),
                            worktree=data.get("worktree", ""),
                        )
                    except KeyError as exc:
                        self._send_json(400, {"error": f"Missing field: {exc}"})
                        return
                    self._send_json(200 if res.get("status") == "OK" else 503, res)
                else:
                    self._send_json(404, {"error": "Not Found"})

        self.server = ThreadingHTTPServer((self.host, self.port), HubRequestHandler)  # M-3
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        if self.keep_alive_enabled:
            self.keep_alive_thread = threading.Thread(target=self._keep_alive_worker, daemon=True)
            self.keep_alive_thread.start()

    def stop(self):
        """Stop the daemon and cleanly release all resources."""
        self.stop_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.db.close()


if __name__ == "__main__":
    token = os.environ.get("TRINITY_HUB_TOKEN")
    daemon = CentralHubDaemon(port=8765, db_path="central_hub.db", auth_token=token)
    daemon.start()
    print(f"Central Hub Daemon started on http://{daemon.host}:{daemon.port}")
    if not token:
        # T-2: never print the secret; console output ends up in logs and transcripts.
        token_path = os.path.join(os.path.expanduser("~"), ".trinity_hub_token")
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(daemon.auth_token)
        print(f"Bearer token written to {token_path} (set TRINITY_HUB_TOKEN to pin)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopping Central Hub Daemon...")
        daemon.stop()
