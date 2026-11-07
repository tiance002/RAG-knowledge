"""Actual isolated runtime and frozen guard, with synthetic identities/HTTP only.

This is a present-path reproduction, never a diagnosis of the historical stop.
Run in a fresh child because configure_runtime must precede application imports.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_actual_runtime_redis_and_both_sdk_bindings(tmp_path):
    environment = {**os.environ, "M2_AB_API_KEY": "synthetic-only-never-a-real-key",
                   "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--synthetic-child", str(tmp_path)],
        cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {"status": "PASS", "synthetic_transport_calls": 2,
                      "external_socket_connections": 0, "historical_cause": "UNKNOWN"}


def synthetic_child(output):
    import asyncio
    from copy import deepcopy
    import importlib.util
    import socket

    import httpx

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    external_connections = []

    def check(address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            external_connections.append(str(address))
            raise AssertionError("Synthetic integration forbids external sockets")

    def connect(sock, address, *args, **kwargs):
        check(address)
        return original_connect(sock, address, *args, **kwargs)

    def connect_ex(sock, address, *args, **kwargs):
        check(address)
        return original_connect_ex(sock, address, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    module = importlib.util.spec_from_file_location(
        "synthetic_supplement_actual_guard", ROOT / "scripts/m2_current_supplement.py")
    runner = importlib.util.module_from_spec(module)
    module.loader.exec_module(runner)
    spec = json.loads((runner.P / "m2-current-supplement-manifest-v1.json").read_bytes())
    _, _, fresh, _, _ = runner.validate(spec)
    fresh = deepcopy(fresh)
    synthetic_qid = "synthetic-only:actual-runtime-sdk-probe"
    fresh["cases"] = [{"qid": synthetic_qid}]
    fresh["dataset_version"] = "synthetic-only-runtime-probe-v1"
    grant_before = runner.GRANT.read_bytes()
    lease_before = runner.recon.r.LEASE.read_bytes()
    sent = []

    async def respond(request):
        # The real SDK serialized this request and the actual guard bounded and
        # durably reserved it. The response is entirely fabricated in memory.
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload["model"] == fresh["model"]
        return httpx.Response(200, request=request, json={
            "id": "synthetic-only-" + str(len(sent)), "object": "chat.completion",
            "created": 0, "model": fresh["model"],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic reply"},
                         "finish_reason": "stop"}],
        })

    guard = runner.Transport(fresh, output, spec, grant_before,
                             transport=httpx.MockTransport(respond))
    guard.context = {"qid": synthetic_qid, "mode": "chat", "arm": "A", "repetition": 1,
                     "dataset_version": fresh["dataset_version"], "run_id": "synthetic-only"}
    # This creates its own loopback Redis. It never opens configured application
    # Redis, copies a real index or changes any durable authorization artifact.
    with runner.base.isolated_redis(output) as redis_url:
        runner.base.configure_runtime(fresh, output, redis_url)
        runner.safety.assert_runtime_isolation(output, redis_url)

        async def exercise():
            from app.services.llm import LLMService
            from app.services.memory import get_memory_service, PreferenceScope

            memory = get_memory_service()
            scope = PreferenceScope(tenant_id="synthetic-only", user_id="runtime-probe")
            try:
                await memory.update_preference({"answer_style": "concise"}, scope=scope)
                assert (await memory.get_preferences(scope=scope)).values.answer_style == "concise"
                async with guard.bind_services():
                    service = LLMService()
                    response = await service._get_client().chat.completions.create(
                        model=service.model, messages=[{"role": "user", "content": "Synthetic SDK probe"}],
                        max_tokens=64, stream=False,
                    )
                    assert response.choices[0].message.content == "synthetic reply"
                    guard.context = {**guard.context, "mode": "agent"}
                    model = service.get_chat_model(streaming=False, max_tokens=64)
                    response = await model.ainvoke("Synthetic LangChain probe")
                    assert response.content == "synthetic reply"
            finally:
                await memory.client.aclose()

        with guard.installed():
            asyncio.run(exercise())

    assert len(sent) == len(guard.calls) == 2
    assert all(row["state"] == "COMPLETED" for row in guard.calls)
    assert not external_connections
    assert runner.GRANT.read_bytes() == grant_before
    assert runner.recon.r.LEASE.read_bytes() == lease_before
    print(json.dumps({"status": "PASS", "synthetic_transport_calls": len(sent),
                      "external_socket_connections": len(external_connections),
                      "historical_cause": "UNKNOWN"}))


if __name__ == "__main__":
    assert sys.argv[1] == "--synthetic-child"
    synthetic_child(Path(sys.argv[2]).resolve())
