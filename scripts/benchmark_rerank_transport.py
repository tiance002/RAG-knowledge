"""Synthetic-only rerank transport timing; no answer-quality/production claims.

Run with an existing backend environment. The provider is a local HTTP/1.1
server returning fixed scores after a fixed delay. No database, index, real
model, or external DNS/connection is needed. The output directory must be new.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def loopback_only():
    """Also permit Windows asyncio's internal numeric-loopback socketpair."""
    dns, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    denied = []

    def allowed(value):
        try:
            return ipaddress.ip_address(value.decode() if isinstance(value, bytes) else value).is_loopback
        except (ValueError, TypeError):
            return False

    def local_dns(host, *args, **kwargs):
        if not allowed(host):
            denied.append("external_dns")
            raise AssertionError("External DNS forbidden in synthetic benchmark")
        return dns(host, *args, **kwargs)

    def guarded(method):
        def call(sock, address):
            if not isinstance(address, tuple) or not allowed(address[0]):
                denied.append("external_connection")
                raise AssertionError("External connection forbidden in synthetic benchmark")
            return method(sock, address)
        return call

    socket.getaddrinfo = local_dns
    socket.socket.connect = guarded(connect)
    socket.socket.connect_ex = guarded(connect_ex)
    try:
        yield denied
    finally:
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = dns, connect, connect_ex


class SyntheticServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, delay):
        self.delay = delay
        self.records = []
        self.lock = threading.Lock()
        self.connections = 0
        super().__init__(("127.0.0.1", 0), SyntheticHandler)


class SyntheticHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self.server.lock:
            self.server.connections += 1
            self.connection_id = self.server.connections

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        started = time.perf_counter()
        time.sleep(self.server.delay)
        valid = self.path == "/v1/rerank"
        data = {"results": [{"index": i, "relevance_score": .95 - i * .1}
                            for i in range(len(payload.get("documents", [])))]} if valid else {"error": "Unexpected endpoint"}
        body = json.dumps(data).encode()
        with self.server.lock:
            self.server.records.append({"request_number": len(self.server.records) + 1,
                "connection_id": self.connection_id, "path": self.path, "payload": payload,
                "simulated_provider_ms": (time.perf_counter() - started) * 1000})
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


async def measure(service, model, asynchronous, trials, callback_delay):
    rows = []
    loop = asyncio.get_running_loop()
    for trial in range(1, trials + 1):
        callback_done = asyncio.Event()
        started = time.perf_counter()
        scheduled = started + callback_delay
        callback = {}

        def lightweight():
            callback["actual_ms"] = (time.perf_counter() - started) * 1000
            callback["lag_ms"] = max(0.0, (time.perf_counter() - scheduled) * 1000)
            callback_done.set()

        handle = loop.call_later(callback_delay, lightweight)
        try:
            if asynchronous:
                result = await service.arerank("synthetic question", ["synthetic alpha", "synthetic beta"], model)
            else:
                result = service.rerank("synthetic question", ["synthetic alpha", "synthetic beta"], model)
            complete = (time.perf_counter() - started) * 1000
            await asyncio.wait_for(callback_done.wait(), timeout=2)
            rows.append({"trial": trial, "phase": "cold" if trial == 1 else "subsequent",
                         "completion_ms": complete, "callback_scheduled_ms": callback_delay * 1000,
                         "callback_actual_ms": callback["actual_ms"], "callback_lag_ms": callback["lag_ms"],
                         "result": result})
        finally:
            handle.cancel()
    return rows


async def exercise(port, trials, callback_delay):
    from app.services.provider_http import managed_provider_http
    from app.services.rerank import RerankService
    service = RerankService(None)
    model = SimpleNamespace(base_url=f"http://127.0.0.1:{port}/v1", api_key="synthetic-only", model_name="synthetic-rerank")
    legacy = await measure(service, model, False, trials, callback_delay)
    async with managed_provider_http():
        candidate = await measure(service, model, True, trials, callback_delay)
    return legacy, candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--delay-ms", type=float, default=100)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--callback-ms", type=float, default=10)
    args = parser.parse_args()
    if args.trials < 2 or not 0 < args.callback_ms < args.delay_ms <= 5000:
        parser.error("Require trials >= 2 and 0 < callback-ms < delay-ms <= 5000")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    # Never export benchmark content through an inherited observation configuration.
    os.environ["RAG_OBSERVABILITY_ENABLED"] = "false"
    os.environ["LANGFUSE_ENABLED"] = "false"
    report = {"schema_version": 1, "kind": "SYNTHETIC_TRANSPORT_ONLY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_boundary": "Synthetic callback scheduling and loopback HTTP connection reuse only; no answer-quality, clinical, production-latency, throughput, or P95 claim.",
        "configuration": {"delay_ms": args.delay_ms, "trials_per_mode": args.trials,
                          "callback_ms": args.callback_ms, "trial_order": "all retained synchronous trials, then all managed asynchronous trials"},
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "source_sha256": {name: digest(ROOT / name) for name in (
            "scripts/benchmark_rerank_transport.py", "backend/app/services/rerank.py",
            "backend/app/services/provider_http.py", "backend/app/services/llm.py")}}
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    report["git_commit"] = git.stdout.strip() if git.returncode == 0 else None
    report["git_state"] = "Working-tree source hashes identify candidate; commit alone is not candidate identity."
    server = SyntheticServer(args.delay_ms / 1000)
    worker = threading.Thread(target=server.serve_forever, daemon=True, name="synthetic-rerank-http")
    worker.start()
    exit_code = 1
    try:
        with loopback_only() as denied:
            legacy, candidate = asyncio.run(exercise(server.server_port, args.trials, args.callback_ms / 1000))
        requests = list(server.records)
        expected_payload = {"model": "synthetic-rerank", "query": "synthetic question",
                            "documents": ["synthetic alpha", "synthetic beta"], "top_n": 2, "return_documents": False}
        expected_results = [{"index": i, "score": .95 - i * .1, "text": text}
                            for i, text in enumerate(expected_payload["documents"])]
        candidate_requests = requests[args.trials:]
        checks = {"request_count": len(requests) == args.trials * 2,
                  "payload_equivalence": all(row["payload"] == expected_payload for row in requests),
                  "result_equivalence": all(row["result"] == expected_results for row in legacy + candidate),
                  "managed_last_two_connection_reused": len(candidate_requests) >= 2 and candidate_requests[-1]["connection_id"] == candidate_requests[-2]["connection_id"],
                  "no_external_network_attempt": not denied,
                  "only_rerank_endpoint": all(row["path"] == "/v1/rerank" for row in requests)}
        report.update(status="PASS" if all(checks.values()) else "FAIL", checks=checks,
                      legacy_sync=legacy, candidate_async=candidate, server_requests=requests,
                      denied_network_attempts=denied)
        exit_code = 0 if all(checks.values()) else 1
    except BaseException as exc:
        report.update(status="FAIL", error_type=type(exc).__name__, error=str(exc), server_requests=list(server.records))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        report["server_stopped"] = not worker.is_alive()
        report["exit_code"] = exit_code
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "exit_code": exit_code,
                      "report": str((args.output_dir / "report.json").resolve()),
                      "checks": report.get("checks"), "server_stopped": report["server_stopped"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
