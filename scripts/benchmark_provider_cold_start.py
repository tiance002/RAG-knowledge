"""Fresh-process synthetic provider cold-start diagnostics (no external provider).

A: current lazy managed pool. B: diagnostic lifespan preinitialization without
network. C: B plus one same-origin loopback health request. Each repetition is
a separate interpreter, with three identical business requests. Optional D uses
the actual managed_provider_http(preinitialize=True) lifecycle and audits unused
reserve cleanup. Default arms remain A/B/C. Timings are
nested intervals and must not be summed. Optional idle comparison waits once
and compares 30/60-second keepalive expiry on loopback only.
"""
from __future__ import annotations

import argparse
import asyncio
import builtins
from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import ssl
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from benchmark_rerank_transport import SyntheticHandler, SyntheticServer, loopback_only

SOURCES = ("scripts/benchmark_provider_cold_start.py", "scripts/benchmark_rerank_transport.py",
           "backend/app/services/provider_http.py", "backend/app/services/rerank.py", "backend/app/services/llm.py")
PHASE = contextvars.ContextVar("cold_start_phase", default="bootstrap")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def modules():
    return {name: name in sys.modules for name in ("httpx", "httpcore", "openai", "ssl")}


class Handler(SyntheticHandler):
    def do_GET(self):
        body = b'{"status":"synthetic"}'
        with self.server.lock:
            self.server.records.append({"request_number": len(self.server.records) + 1,
                "connection_id": self.connection_id, "path": self.path, "method": "GET"})
        self.send_response(200 if self.path == "/v1/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class Instrumentation:
    def __init__(self):
        self.origin = time.perf_counter()
        self.intervals = []
        self.imports = []
        self.traces = []
        self.requests = []
        self.stack = threading.local()
        self.import_seen = set()
        self.restore = []

    @contextmanager
    def interval(self, name, **extra):
        stack = getattr(self.stack, "rows", [])
        row = {"id": len(self.intervals), "name": name, "phase": PHASE.get(),
               "parent_id": stack[-1]["id"] if stack else None,
               "start_ms": (time.perf_counter() - self.origin) * 1000, **extra}
        self.intervals.append(row)
        self.stack.rows = stack + [row]
        started = time.perf_counter()
        try:
            yield row
        except BaseException as exc:
            row["error_type"] = type(exc).__name__
            raise
        finally:
            row["duration_ms"] = (time.perf_counter() - started) * 1000
            self.stack.rows = stack

    def patch(self, target, name, value):
        self.restore.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def start(self):
        original_import = builtins.__import__
        def imported(name, *args, **kwargs):
            if name not in ("httpx", "httpcore") or name in self.import_seen:
                return original_import(name, *args, **kwargs)
            self.import_seen.add(name)
            before = modules()
            with self.interval("first_import." + name, loaded_before=before) as row:
                value = original_import(name, *args, **kwargs)
                row["loaded_after"] = modules()
            self.imports.append(row)
            return value
        self.patch(builtins, "__import__", imported)
        original_context = ssl.create_default_context
        def context(*args, **kwargs):
            with self.interval("ssl.create_default_context"):
                return original_context(*args, **kwargs)
        self.patch(ssl, "create_default_context", context)
        original_load = ssl.SSLContext.load_verify_locations
        def load(instance, *args, **kwargs):
            with self.interval("ssl.SSLContext.load_verify_locations"):
                return original_load(instance, *args, **kwargs)
        self.patch(ssl.SSLContext, "load_verify_locations", load)

    def instrument_httpx(self):
        import httpx
        original_init, original_send = httpx.AsyncClient.__init__, httpx.AsyncClient.send
        def construct(instance, *args, **kwargs):
            with self.interval("httpx.AsyncClient.__init__", modules_before=modules()) as row:
                original_init(instance, *args, **kwargs)
                row["modules_after"] = modules()
        self.patch(httpx.AsyncClient, "__init__", construct)
        async def send(instance, request, *args, **kwargs):
            request_id = len(self.requests)
            row = {"id": request_id, "phase": PHASE.get(), "path": request.url.path,
                   "start_ms": (time.perf_counter() - self.origin) * 1000,
                   "timeout": request.extensions.get("timeout")}
            self.requests.append(row)
            async def trace(name, info):
                self.traces.append({"request_id": request_id, "phase": row["phase"], "event": name,
                    "at_ms": (time.perf_counter() - self.origin) * 1000,
                    "info_keys": sorted(str(key) for key in info),
                    "error_type": type(info["exception"]).__name__ if info.get("exception") else None})
            request.extensions["trace"] = trace
            started = time.perf_counter()
            try:
                response = await original_send(instance, request, *args, **kwargs)
                row["status_code"] = response.status_code
                return response
            except BaseException as exc:
                row["error_type"] = type(exc).__name__
                raise
            finally:
                row["duration_ms"] = (time.perf_counter() - started) * 1000
        self.patch(httpx.AsyncClient, "send", send)

    def stop(self):
        for target, name, old in reversed(self.restore):
            setattr(target, name, old)


async def timed_phase(instrument, name, operation, callback_ms):
    token = PHASE.set(name)
    started = time.perf_counter()
    callback_done = asyncio.Event()
    callback = {}
    def lightweight():
        callback["actual_ms"] = (time.perf_counter() - started) * 1000
        callback["lag_ms"] = max(0.0, callback["actual_ms"] - callback_ms)
        callback_done.set()
    handle = asyncio.get_running_loop().call_later(callback_ms / 1000, lightweight)
    try:
        with instrument.interval("phase." + name, modules_before=modules()) as row:
            value = await operation()
            complete = (time.perf_counter() - started) * 1000
            row["operation_ms"] = complete
            row["modules_after"] = modules()
        wait_started = time.perf_counter()
        await asyncio.wait_for(callback_done.wait(), 3)
        callback_wait_ms = (time.perf_counter() - wait_started) * 1000
        return {"callback_instrumentation_wait_ms": callback_wait_ms, "name": name, "operation_ms": complete, "callback_scheduled_ms": callback_ms,
                "callback_actual_ms": callback["actual_ms"], "callback_lag_ms": callback["lag_ms"], "result": value}
    finally:
        handle.cancel()
        PHASE.reset(token)


async def business(instrument, arm, server, args):
    bootstrap_started = time.perf_counter()
    with instrument.interval("backend_imports", modules_before=modules()) as bootstrap:
        from app.services.provider_http import managed_provider_http, provider_request
        from app.services.rerank import RerankService
        from types import SimpleNamespace
        bootstrap["modules_after"] = modules()
    instrument.instrument_httpx()
    model = SimpleNamespace(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="synthetic-only", model_name="synthetic-rerank")
    service = RerankService(None)
    phases = []
    async def preinit():
        before = len(server.records)
        async with provider_request(base_url=model.base_url, api_key=model.api_key, stage="rerank", timeout=120.0):
            pass
        return {"network_request_count": len(server.records) - before}
    async def health():
        async with provider_request(base_url=model.base_url, api_key=model.api_key, stage="rerank", timeout=120.0) as client:
            response = await client.get(model.base_url + "/health")
            response.raise_for_status()
            return {"status_code": response.status_code}
    async def execute_business():
        if arm in ("B", "C"):
            phases.append(await timed_phase(instrument, "preinit_no_network", preinit, args.callback_ms))
        if arm == "C":
            phases.append(await timed_phase(instrument, "same_origin_health", health, args.callback_ms))
        startup_ms = (time.perf_counter() - bootstrap_started) * 1000
        startup_callback_wait_ms = sum(row["callback_instrumentation_wait_ms"] for row in phases)
        startup = {"before_business_ms": startup_ms, "callback_instrumentation_wait_ms": startup_callback_wait_ms,
            "excluding_callback_wait_ms": startup_ms - startup_callback_wait_ms,
            "semantics": "before_business_ms includes callback-wait instrumentation; excluding_callback_wait_ms subtracts only those explicit waits, and backend_imports/preinit/health intervals remain independently inspectable."}
        for trial in range(1, 4):
            async def request():
                return await service.arerank("synthetic question", ["synthetic alpha", "synthetic beta"], model)
            phases.append(await timed_phase(instrument, f"business_{trial}", request, args.callback_ms))
        return startup

    reserve_audit = None
    if arm == "D":
        lifecycle = managed_provider_http(preinitialize=True)
        manager = None
        initial_reserves = []
        error = (None, None, None)
        try:
            async def actual_startup():
                nonlocal manager, initial_reserves
                before = len(server.records)
                manager = await lifecycle.__aenter__()
                initial_reserves = list(manager.reserves.values())
                return {"network_request_count": len(server.records) - before,
                    "reserve_count": len(initial_reserves), "reserve_shapes": [
                        {"timeout": shape[0], "verify": shape[1], "trust_env": shape[2]}
                        for shape in manager.reserves]}
            phases.append(await timed_phase(instrument, "actual_lifespan_preinitialize", actual_startup, args.callback_ms))
            startup = await execute_business()
        except BaseException:
            error = sys.exc_info()
            raise
        finally:
            if manager is not None:
                unused = list(manager.reserves.values())
                claimed = list(manager.clients.values())
                async def actual_cleanup():
                    await lifecycle.__aexit__(*error)
                    return {"manager_closed": manager.closed,
                        "unused_reserve_count": len(unused), "unused_closed": [client.is_closed for client in unused],
                        "claimed_count": len(claimed), "claimed_closed": [client.is_closed for client in claimed],
                        "initial_reserves_closed": [client.is_closed for client in initial_reserves],
                        "remaining_pool_count": len(manager.clients), "remaining_reserve_count": len(manager.reserves)}
                cleanup = await timed_phase(instrument, "actual_lifespan_cleanup", actual_cleanup, args.callback_ms)
                phases.append(cleanup)
                reserve_audit = cleanup["result"]
    else:
        # Preserve the original lazy A/B/C lifecycle, including manual B/C leases.
        async with managed_provider_http():
            startup = await execute_business()
    return phases, startup, reserve_audit



async def idle_comparison(instrument, server, args):
    import httpx
    instrument.instrument_httpx()
    url = f"http://127.0.0.1:{server.server_port}/v1/rerank"
    payload = {"model": "synthetic-rerank", "query": "synthetic question", "documents": ["synthetic alpha", "synthetic beta"], "top_n": 2, "return_documents": False}
    clients = [httpx.AsyncClient(trust_env=False, limits=httpx.Limits(keepalive_expiry=expiry)) for expiry in (30, 60)]
    rows = []
    try:
        for expiry, client in zip((30, 60), clients):
            response = await client.post(url, json=payload)
            response.raise_for_status()
            rows.append({"expiry_seconds": expiry, "before_connection_id": server.records[-1]["connection_id"], "initial_response_completed_at_ms": (time.perf_counter() - instrument.origin) * 1000})
        started = time.perf_counter()
        await asyncio.sleep(args.idle_seconds)
        waited = time.perf_counter() - started
        for row, client in zip(rows, clients):
            row["actual_idle_seconds"] = (time.perf_counter() - instrument.origin - row["initial_response_completed_at_ms"] / 1000)
            response = await client.post(url, json=payload)
            response.raise_for_status()
            row.update(after_connection_id=server.records[-1]["connection_id"], shared_sleep_seconds=waited)
            row["reused"] = row["before_connection_id"] == row["after_connection_id"]
    finally:
        for client in clients:
            await client.aclose()
    return rows


def child(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    os.environ["RAG_OBSERVABILITY_ENABLED"] = "false"
    os.environ["LANGFUSE_ENABLED"] = "false"
    instrument = Instrumentation()
    report = {"arm": args.child, "pid": os.getpid(), "source_sha256": hashes(), "initial_modules": modules(),
        "configuration": {"delay_ms": args.delay_ms, "callback_ms": args.callback_ms, "business_requests": 3},
        "environment": {"python": platform.python_version(), "openssl": ssl.OPENSSL_VERSION,
            "package_versions": {name: importlib.metadata.version(name) for name in ("httpx", "httpcore", "certifi", "openai")}},
        "timing_semantics": "Nested inclusive wall-clock intervals; do not add parent/child SSL/import/client intervals. Operation excludes waiting for a scheduled callback after completion.",
        "transport_tls": "NOT_RUN: loopback origin is HTTP; SSL context initialization is measured, TLS handshake/network latency is not."}
    server = SyntheticServer(args.delay_ms / 1000)
    server.RequestHandlerClass = Handler
    worker = threading.Thread(target=server.serve_forever, daemon=True, name="cold-start-loopback")
    worker.start()
    instrument.start()
    import certifi
    report["environment"]["certifi_ca"] = {"path": certifi.where(), "sha256": hashlib.sha256(Path(certifi.where()).read_bytes()).hexdigest()}
    code = 1
    try:
        with loopback_only() as denied:
            if args.child == "idle":
                report["idle"] = asyncio.run(idle_comparison(instrument, server, args))
            else:
                report["phases"], report["startup"], report["reserve_cleanup"] = asyncio.run(business(instrument, args.child, server, args))
        report["denied_network_attempts"] = denied
        report["status"] = "PASS" if not denied else "FAIL"
        code = 0 if not denied else 1
    except BaseException as exc:
        report.update(status="FAIL", error_type=type(exc).__name__, error=str(exc))
    finally:
        instrument.stop()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        report.update(server_stopped=not worker.is_alive(), server_requests=list(server.records),
            intervals=instrument.intervals, first_imports=instrument.imports,
            httpcore_trace=instrument.traces, httpx_requests=instrument.requests, exit_code=code)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "arm": args.child, "exit_code": code}))
    return code


def orchestrate(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    plan = {"created_at": datetime.now(timezone.utc).isoformat(), "source_sha256": hashes(),
        "kind": "SYNTHETIC_DIAGNOSTIC_ONLY", "repetitions_per_arm": args.repetitions, "arms": args.arms,
        "cold_semantics": "Process-fresh interpreters; OS filesystem/page caches are not cleared. This is not machine-cold.",
        "claim_boundary": "No answer-quality, clinical, production latency, throughput or P95 claim; B/C are diagnostic lifecycle actions only, with no production changes.",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "configuration": {"delay_ms": args.delay_ms, "callback_ms": args.callback_ms, "business_requests_per_child": 3}}
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    reports = []
    jobs = [(arm, rep) for rep in range(1, args.repetitions + 1) for arm in args.arms]
    if args.idle_seconds is not None:
        jobs.append(("idle", 1))
    for arm, rep in jobs:
        destination = args.output_dir / f"{arm}-{rep}"
        command = [sys.executable, str(Path(__file__).resolve()), "--child", arm,
            "--output-dir", str(destination), "--delay-ms", str(args.delay_ms), "--callback-ms", str(args.callback_ms)]
        if arm == "idle":
            command += ["--idle-seconds", str(args.idle_seconds)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
        (args.output_dir / f"{arm}-{rep}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        report = json.loads((destination / "report.json").read_text(encoding="utf-8")) if (destination / "report.json").exists() else {"status": "FAIL", "error": "Missing child report"}
        reports.append({"arm": arm, "repetition": rep, "command": command, "process_exit_code": result.returncode, "report": report})
        print(json.dumps({"finished": f"{arm}-{rep}", "exit_code": result.returncode}), flush=True)
    business_reports = [entry["report"] for entry in reports if entry["arm"] != "idle"]
    expected_payload = {"model": "synthetic-rerank", "query": "synthetic question", "documents": ["synthetic alpha", "synthetic beta"], "top_n": 2, "return_documents": False}
    expected_result = [{"index": i, "score": .95 - i * .1, "text": text} for i, text in enumerate(expected_payload["documents"])]
    checks = {"all_children_succeeded": all(entry["process_exit_code"] == 0 and entry["report"]["status"] == "PASS" for entry in reports),
        "fresh_process_per_repetition": len({entry["report"].get("pid") for entry in reports}) == len(reports),
        "identical_sources": all(entry["report"].get("source_sha256") == plan["source_sha256"] for entry in reports),
        "three_business_requests_each": all(len([r for r in report.get("server_requests", []) if r["path"] == "/v1/rerank"]) == 3 for report in business_reports),
        "payload_matches_expected": all(row.get("payload") == expected_payload for report in business_reports for row in report.get("server_requests", []) if row["path"] == "/v1/rerank"),
        "results_match_expected": all(phase["result"] == expected_result for report in business_reports for phase in report.get("phases", []) if phase["name"].startswith("business_")),
        "tcp_trace_present": all(any("connect_tcp.complete" in row["event"] for row in report.get("httpcore_trace", [])) for report in business_reports),
        "managed_last_two_connections_reused": all(len({row["connection_id"] for row in report.get("server_requests", []) if row["path"] == "/v1/rerank"} ) == 1 for report in business_reports),
        "same_origin_health_connection_reused": all(len({row["connection_id"] for row in report.get("server_requests", [])}) == 1 for report in business_reports if report["arm"] == "C"),
        "idle_expiry_observed_as_expected": all(row["reused"] == (row["actual_idle_seconds"] < row["expiry_seconds"]) for entry in reports if entry["arm"] == "idle" for row in entry["report"].get("idle", [])),
        "same_payloads": len({json.dumps(row.get("payload"), sort_keys=True) for report in business_reports for row in report.get("server_requests", []) if row["path"] == "/v1/rerank"}) == 1,
        "same_results": len({json.dumps(phase["result"], sort_keys=True) for report in business_reports for phase in report.get("phases", []) if phase["name"].startswith("business_")}) == 1,
        "preinit_has_no_send": all(phase["result"]["network_request_count"] == 0 for report in business_reports for phase in report.get("phases", []) if phase["name"] == "preinit_no_network"),
        "health_only_in_C": all(sum(row["path"] == "/v1/health" for row in report.get("server_requests", [])) == (1 if report["arm"] == "C" else 0) for report in business_reports),
        "actual_preinit_has_no_send": all(phase["result"]["network_request_count"] == 0 for report in business_reports if report["arm"] == "D" for phase in report.get("phases", []) if phase["name"] == "actual_lifespan_preinitialize"),
        "actual_preinit_four_reserves": all(phase["result"]["reserve_count"] == 4 for report in business_reports if report["arm"] == "D" for phase in report.get("phases", []) if phase["name"] == "actual_lifespan_preinitialize"),
        "actual_preinit_no_business_constructor": all(not any(row["name"] == "httpx.AsyncClient.__init__" and row["phase"].startswith("business_") for row in report.get("intervals", [])) for report in business_reports if report["arm"] == "D"),
        "actual_preinit_unused_reserves_closed": all(report.get("reserve_cleanup") is not None and report["reserve_cleanup"]["unused_reserve_count"] == 3 and all(report["reserve_cleanup"]["unused_closed"]) for report in business_reports if report["arm"] == "D"),
        "actual_preinit_all_clients_closed": all(report.get("reserve_cleanup") is not None and report["reserve_cleanup"]["claimed_count"] == 1 and all(report["reserve_cleanup"]["initial_reserves_closed"]) and all(report["reserve_cleanup"]["claimed_closed"]) and report["reserve_cleanup"]["manager_closed"] and report["reserve_cleanup"]["remaining_pool_count"] == report["reserve_cleanup"]["remaining_reserve_count"] == 0 for report in business_reports if report["arm"] == "D"),
        "all_servers_stopped": all(entry["report"].get("server_stopped") for entry in reports)}
    summary = {"plan": plan, "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "children": reports}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"status": summary["status"], "summary": str((args.output_dir / "summary.json").resolve()), "checks": checks}), flush=True)
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--arms", nargs="+", choices=("A", "B", "C", "D"), default=["A", "B", "C"])
    parser.add_argument("--delay-ms", type=float, default=100)
    parser.add_argument("--callback-ms", type=float, default=10)
    parser.add_argument("--idle-seconds", type=float)
    parser.add_argument("--child", choices=("A", "B", "C", "D", "idle"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if len(set(args.arms)) != len(args.arms):
        parser.error("Arms must be unique")
    if args.repetitions < 3 or not 0 < args.callback_ms < args.delay_ms <= 5000:
        parser.error("Require repetitions >= 3 and 0 < callback-ms < delay-ms <= 5000")
    if args.idle_seconds is not None and not 30 < args.idle_seconds < 60:
        parser.error("Idle comparison must wait more than 30 and less than 60 seconds")
    return child(args) if args.child else orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
