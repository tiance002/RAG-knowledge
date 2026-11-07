"""Current HTTP adapter contracts and real local transport with fake provider.

Every integration identity is synthetic and every provider response is an
in-memory fixture. Historical grants/ledgers remain immutable.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_runner():
    spec = importlib.util.spec_from_file_location("current_http_under_test", ROOT / "scripts/m2_current_http_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_contract_retains_budget_unknowns_and_two_disjoint_modes():
    runner = load_runner()
    spec = runner.make_spec()
    _, _, fresh, audit, policy = runner.validate(spec)
    assert audit["committed_cny"] == "1.7944776"
    assert audit["remaining_cny"] == "0.2055224"
    assert [item["mode"] for item in spec["identities"]] == ["chat", "agent"]
    assert {item["arm"] for item in spec["identities"]} == {"D"}
    assert len({item["run_identity"] for item in spec["identities"]}) == 2
    assert fresh["cases"][0]["expected_chunk_ids"] == ["word-paragraph-6"]


@pytest.mark.parametrize("field,value", [
    ("runner_sha256", "0" * 64), ("max_new_http_requests", 3),
    ("authorization_id", "synthetic-unapproved"), ("fixed_search", {}),
])
def test_changed_authorization_configuration_fails_closed(field, value):
    runner = load_runner()
    spec = runner.make_spec()
    spec[field] = value
    with pytest.raises(ValueError):
        runner.validate(spec)


def test_changed_or_duplicate_http_identity_is_rejected():
    runner = load_runner()
    spec = runner.make_spec()
    spec["identities"][1] = deepcopy(spec["identities"][0])
    with pytest.raises(ValueError):
        runner.validate(spec)


def test_atomic_grant_cannot_be_claimed_twice(tmp_path, monkeypatch):
    runner = load_runner()
    monkeypatch.setattr(runner, "GRANT", tmp_path / "synthetic.started")
    first = runner.claim({"synthetic": True}, tmp_path / "one")
    with pytest.raises(FileExistsError):
        runner.claim({"synthetic": True}, tmp_path / "two")
    assert runner.GRANT.read_bytes() == first


def synthetic_guard(tmp_path, monkeypatch):
    import httpx
    runner = load_runner()
    monkeypatch.setattr(runner, "validate", lambda value: None)
    monkeypatch.setattr(runner, "GRANT", tmp_path / "synthetic.started")
    runner.GRANT.write_bytes(b"owned-synthetic")
    identity = {"qid": "synthetic-only:guard", "mode": "chat", "arm": "D", "repetition": 1,
                "run_identity": "synthetic-only:guard-chat"}
    manifest = {"base_url": "https://api.siliconflow.cn/v1", "model": "zai-org/GLM-5.3-Flash",
                "budget_cny": str(runner.REMAINING), "max_output_tokens": 4096,
                "pricing": {"input_cny_per_million": ".8", "output_cny_per_million": "2.8"}}
    guard = runner.Transport(manifest, tmp_path, {"identities": [identity]}, b"owned-synthetic")
    guard.context = deepcopy(identity)
    request = httpx.Request("POST", runner.base.endpoint(manifest["base_url"]),
                           json={"model": manifest["model"], "messages": []})
    return runner, guard, request


@pytest.mark.parametrize("reason", ["grant", "identity", "history", "recorded", "budget"])
def test_dispatch_fences_send_nothing(tmp_path, monkeypatch, reason):
    import asyncio
    import httpx
    runner, guard, request = synthetic_guard(tmp_path, monkeypatch)
    if reason == "grant":
        runner.GRANT.write_bytes(b"changed")
    elif reason == "identity":
        guard.context["mode"] = "agent"
    elif reason == "history":
        def changed(value):
            raise ValueError("synthetic changed history")
        monkeypatch.setattr(runner, "validate", changed)
    elif reason == "recorded":
        (tmp_path / "trials.jsonl").write_bytes(runner.base.canonical(guard.context) + b"\n")
    else:
        request = httpx.Request("POST", guard.url, json={"model": guard.manifest["model"],
            "messages": [{"role": "user", "content": "x" * 100000}]})
    async def forbidden(*args, **kwargs):
        pytest.fail("Dispatch fence must prevent transport invocation")
    with pytest.raises(runner.base.GuardStop):
        asyncio.run(guard.send(forbidden, None, request))
    assert not guard.calls and guard.committed == 0


def test_unknown_retains_full_reservation_and_cannot_retry(tmp_path, monkeypatch):
    import asyncio
    import httpx
    runner, guard, request = synthetic_guard(tmp_path, monkeypatch)
    calls = []
    async def timeout(*args, **kwargs):
        calls.append(True)
        raise httpx.ReadTimeout("synthetic-only")
    with pytest.raises(runner.base.GuardStop):
        asyncio.run(guard.send(timeout, None, request))
    assert guard.calls[0]["state"] == "UNKNOWN"
    assert guard.committed == runner.Decimal(guard.calls[0]["reservation_cny"])
    with pytest.raises(runner.base.GuardStop):
        asyncio.run(guard.send(timeout, None, request))
    assert calls == [True]


@pytest.mark.parametrize("cloud", [False, True])
def test_actual_tcp_sse_redis_sdk_and_optional_redacted_export(tmp_path, cloud):
    environment = {**os.environ, "M2_AB_API_KEY": "synthetic-only-never-real",
                   "LANGFUSE_PUBLIC_KEY": "pk-synthetic-only-memory-export",
                   "LANGFUSE_SECRET_KEY": "synthetic-only-memory-secret",
                   "LANGFUSE_BASE_URL": "https://example.invalid", "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--synthetic-child", str(tmp_path), str(int(cloud))],
        cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=240,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["status"] == "PASS_SYNTHETIC_ONLY"
    assert result["http_trials"] == 2 and result["external_connections"] == 0
    assert result["sdk_spans"] > 0 if cloud else result["sdk_spans"] == 0


def synthetic_child(output, cloud):
    import asyncio
    import socket

    import httpx

    runner = load_runner()
    contract = runner.make_spec()
    original_validate = runner.validate
    history, parent, fresh, audit, policy = original_validate(contract)
    fresh = deepcopy(fresh)
    real_case = deepcopy(fresh["cases"][0])
    synthetic_qid = "synthetic-only:current-http-integration"
    fresh["cases"][0]["qid"] = synthetic_qid
    fresh["dataset_version"] = "synthetic-only-current-http-v1"
    spec = deepcopy(contract)
    for identity in spec["identities"]:
        identity["qid"] = synthetic_qid
        identity["run_identity"] = "synthetic-only:http:" + identity["mode"]
    # Each invocation still verifies the complete immutable actual ancestry.
    # Only this isolated synthetic run's identity namespace is substituted.
    def validate_synthetic(value):
        assert value == spec
        original_validate(contract)
        return history, parent, fresh, audit, policy
    runner.validate = validate_synthetic
    runner.P = output
    runner.GRANT = output / "synthetic-only.started"
    runner.base.private_path = lambda value: Path(value).resolve()
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    external = []
    def check(address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            external.append(str(address))
            raise AssertionError("Synthetic HTTP test forbids external sockets")
    def connect(sock, address, *args, **kwargs):
        check(address)
        return original_connect(sock, address, *args, **kwargs)
    def connect_ex(sock, address, *args, **kwargs):
        check(address)
        return original_connect_ex(sock, address, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    lease_path = runner.source.source.prior.recon.r.LEASE
    lease_before = lease_path.read_bytes()
    seen = []
    async def provider(request):
        payload = json.loads(request.content)
        seen.append(payload)
        messages = payload["messages"]
        joined = str(messages)
        message = {"role": "assistant", "content": fresh["cases"][0]["source_quote"] + " [1]"}
        finish = "stop"
        if "Classify intent as:" in joined:
            message["content"] = "qa"
        elif "文档相关性评分员" in joined:
            message["content"] = "0.95"
        elif payload.get("tools") and not any(item.get("role") == "tool" for item in messages):
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "synthetic-search", "type": "function", "function": {
                    "name": "search_knowledge_base", "arguments": json.dumps({"query": real_case["question"]})}}]}
            finish = "tool_calls"
        body = {"id": "synthetic-provider-" + str(len(seen)), "object": "chat.completion", "created": 0,
                "model": fresh["model"], "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
        if payload.get("stream"):
            first = {**body, "choices": [{"index": 0, "delta": message, "finish_reason": None}], "usage": None}
            last = {**body, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": None}
            tail = {**body, "choices": []}
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                content=b"".join(b"data: " + runner.base.canonical(item) + b"\n\n" for item in (first, last, tail)) + b"data: [DONE]\n\n")
        return httpx.Response(200, request=request, json=body)

    exported = []
    sdk = None
    original_serve = runner.serve
    async def serve_with_memory_export(*args):
        nonlocal sdk
        if cloud:
            from langfuse import Langfuse
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
            from app.services import observability as obs
            exporter = InMemorySpanExporter()
            sdk = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"], secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                tracer_provider=TracerProvider(), span_exporter=exporter,
                mask=lambda **kw: obs.sanitize(kw.get("data"), final_export=True),
                mask_otel_spans=obs._native_mask_spans, should_export_span=obs._native_should_export)
            obs._client, obs._attempted = sdk, True
        try:
            return await original_serve(*args)
        finally:
            if cloud:
                sdk.flush()
                exported.extend(exporter.get_finished_spans())
    runner.serve = serve_with_memory_export
    run = output / "m2-current-http-real-synthetic-only"
    result = runner.execute(spec, run, transport=httpx.MockTransport(provider), cloud=cloud)
    if sdk is not None:
        sdk.shutdown()
    assert result["status"] == "RECORDED_REQUIRES_EVIDENCE_REVIEW", result
    assert result["unknown_calls"] == 0 and result["actual_http_calls"] == len(seen) >= 2
    trials = [json.loads(line) for line in (run / "trials.jsonl").read_bytes().splitlines()]
    assert len(trials) == 2 and [row["mode"] for row in trials] == ["chat", "agent"]
    assert all(row["state"] == "RETURNED" and row["events"][-1]["event"] == "done" for row in trials)
    for mode in ("chat", "agent"):
        raw = (run / ("http-" + mode + ".raw")).read_bytes()
        assert raw.startswith(b"HTTP/1.1 200") and b"text/event-stream" in raw and b"event: done" in raw
    assert any("回答表达偏好" in str(payload["messages"]) for payload in seen)
    assert not external and lease_path.read_bytes() == lease_before
    if cloud:
        assert exported
        text = str([dict(span.attributes) for span in exported])
        assert "memory.preferences" in text and "memory.delivery" in text and "step_by_step" in text
        for private in (real_case["question"], real_case["source_quote"], "synthetic-only-memory-secret"):
            assert private not in text
    print(json.dumps({"status": "PASS_SYNTHETIC_ONLY", "http_trials": len(trials),
                      "external_connections": len(external), "sdk_spans": len(exported)}))


if __name__ == "__main__":
    assert sys.argv[1] == "--synthetic-child"
    synthetic_child(Path(sys.argv[2]).resolve(), bool(int(sys.argv[3])))
