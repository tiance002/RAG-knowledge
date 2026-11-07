"""Offline-only checks for the extra HTTP smoke harness."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket

import httpx
import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
module = importlib.util.spec_from_file_location('http_smoke', ROOT / 'scripts/m2_http_real_smoke.py')
smoke = importlib.util.module_from_spec(module)
module.loader.exec_module(smoke)
SPEC = ROOT / 'var/preferences-acceptance/m2-http-smoke-synthetic-manifest.json'


@pytest.fixture(autouse=True)
def isolate_test_grant_lookup(tmp_path, monkeypatch, request):
    """Use a disposable grant directory for tests of the pre-HTTP snapshot.

    The historical evidence stays read-only and unchanged.  The production
    grant check itself is exercised separately against the real grant below.
    """
    if request.node.name == 'test_production_grant_rejects_unreconciled_history':
        return
    original = smoke.base.private_path
    production_locks = (ROOT / 'var/preferences-acceptance/m2-http-smoke-real-locks').resolve()
    test_locks = tmp_path / 'isolated-real-grants'
    def isolated_private_path(path):
        resolved = Path(path).resolve()
        if resolved == production_locks or resolved.is_relative_to(production_locks):
            return test_locks / resolved.relative_to(production_locks)
        return original(path)
    monkeypatch.setattr(smoke.base, 'private_path', isolated_private_path)


def test_production_grant_rejects_unreconciled_history():
    spec = json.loads(SPEC.read_bytes())
    grant = ROOT / 'var/preferences-acceptance/m2-http-smoke-real-locks' / ('chain-' + spec['history']['parent_manifest_sha256'] + '.started')
    assert grant.exists()
    with pytest.raises(ValueError, match='Persistent HTTP grant requires its exact ledger'):
        smoke.preflight(spec)


def test_pre_http_snapshot_budget_includes_both_continuations_and_r2():
    spec = json.loads(SPEC.read_bytes())
    manifest, case, budget = smoke.preflight(spec)
    assert len(manifest['chunks']) == 80 and case['qid'] == 'office50:031'
    assert budget['prior_run_count'] == 3
    assert budget['prior_combined_committed_cny'] == '0.6967552'
    assert budget['remaining_cny'] == '0.3032448'


def test_preflight_rejects_identity_budget_and_db_tampering():
    spec = json.loads(SPEC.read_bytes())
    spec['identities'][1]['run_identity'] = spec['identities'][0]['run_identity']
    with pytest.raises(ValueError, match='distinct'):
        smoke.preflight(spec)
    spec = json.loads(SPEC.read_bytes()); spec['source_db_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Source DB hash'):
        smoke.preflight(spec)
    spec = json.loads(SPEC.read_bytes()); spec['history']['prior_runs'].pop()
    with pytest.raises(ValueError, match='both ordered'):
        smoke.preflight(spec)


def real_spec():
    spec = json.loads(SPEC.read_bytes())
    spec.update(schema='m2-http-real/v1', purpose='NEW_HTTP_SSE_ONLY', authorization_id='test-only',
                qid='office50:031', model='zai-org/GLM-5.3-Flash', max_output_tokens=4096,
                total_budget_cny='1', fixed_search=smoke.base.FIXED_SEARCH,
                http_runner_sha256=smoke.base.digest((ROOT / 'scripts/m2_http_real_smoke.py').read_bytes()))
    for identity in spec['identities']:
        identity['run_identity'] = 'm2-http-real:test-' + identity['mode']
        identity['repetition'] = 3
    return spec


def test_real_manifest_binds_model_search_budget_and_identity():
    spec = real_spec()
    _, _, budget = smoke.preflight(spec, real=True)
    assert budget['prior_combined_committed_cny'] == '0.6967552'
    for key, invalid in [('model', 'other'), ('max_output_tokens', 8192), ('total_budget_cny', '2'),
                         ('qid', 'office50:001'), ('purpose', 'OTHER')]:
        changed = real_spec(); changed[key] = invalid
        with pytest.raises(ValueError):
            smoke.preflight(changed, real=True)


def test_real_shared_active_collision_and_owner_release(tmp_path, monkeypatch):
    spec = real_spec()
    monkeypatch.setattr(smoke.base, 'ROOT', tmp_path)
    monkeypatch.setattr(smoke.base, 'private_path', lambda p: Path(p).resolve())
    directory = tmp_path / 'var/preferences-acceptance/m2-continuation-locks'; directory.mkdir(parents=True)
    active = directory / (spec['history']['parent_manifest_sha256'] + '.active')
    active.write_bytes(b'other-owner')
    with pytest.raises(FileExistsError):
        with smoke.real_locks(spec, tmp_path / 'out'):
            pytest.fail('must not obtain an existing lease')
    assert active.read_bytes() == b'other-owner'
    active.unlink()
    with smoke.real_locks(spec, tmp_path / 'out') as (retained, check):
        check(); assert active.exists()
        active.write_bytes(b'changed-owner')
        with pytest.raises(ValueError, match='lease'):
            check()
    assert active.read_bytes() == b'changed-owner'


def test_real_persistent_lock_and_active_lease_after_started_call(tmp_path, monkeypatch):
    spec = real_spec()
    monkeypatch.setattr(smoke.base, 'ROOT', tmp_path)
    monkeypatch.setattr(smoke.base, 'private_path', lambda p: Path(p).resolve())
    active = tmp_path / 'var/preferences-acceptance/m2-continuation-locks' / (spec['history']['parent_manifest_sha256'] + '.active')
    with smoke.real_locks(spec, tmp_path / 'out') as (retained, check):
        check(); retained['value'] = True
    assert active.exists()
    with pytest.raises(FileExistsError):
        with smoke.real_locks(spec, tmp_path / 'other'):
            pytest.fail('cannot replay started grant')


def test_real_persistent_grant_prevents_replay_even_after_owned_lease_release(tmp_path, monkeypatch):
    spec = real_spec()
    monkeypatch.setattr(smoke.base, 'ROOT', tmp_path)
    monkeypatch.setattr(smoke.base, 'private_path', lambda p: Path(p).resolve())
    active = tmp_path / 'var/preferences-acceptance/m2-continuation-locks' / (spec['history']['parent_manifest_sha256'] + '.active')
    with smoke.real_locks(spec, tmp_path / 'out') as (_, check):
        check()
    assert not active.exists()
    with pytest.raises(FileExistsError):
        with smoke.real_locks(spec, tmp_path / 'other'):
            pytest.fail('persistent real grant prevents replay')
    assert not active.exists()


def test_offline_guard_rejects_nonloopback_connect_and_connect_ex():
    with smoke.no_external_sockets(), socket.socket() as sock:
        with pytest.raises(RuntimeError, match='non-loopback'):
            sock.connect(('192.0.2.1', 443))
        with pytest.raises(RuntimeError, match='non-loopback'):
            sock.connect_ex(('192.0.2.1', 443))
        with pytest.raises(RuntimeError, match='non-loopback DNS'):
            socket.getaddrinfo('unapproved.invalid', 443)


def test_offline_guard_blocks_udp_and_available_sendmsg_paths():
    with smoke.no_external_sockets(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(RuntimeError, match='non-loopback'):
            sock.sendto(b'x', ('192.0.2.1', 443))
        if hasattr(sock, 'sendmsg'):
            with pytest.raises(RuntimeError, match='non-loopback'):
                sock.sendmsg([b'x'], [], 0, ('192.0.2.1', 443))
        if hasattr(sock, 'sendmsg_afalg'):
            with pytest.raises(RuntimeError, match='AF_ALG'):
                sock.sendmsg_afalg([b'x'])


def test_uncontrolled_httpx_is_rejected_without_network():
    spec = json.loads(SPEC.read_bytes())
    manifest, _, budget = smoke.preflight(spec)
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        guard = smoke.base.BudgetTransport({**manifest, 'budget_cny': budget['remaining_cny']}, directory)
        async def send():
            async with httpx.AsyncClient(trust_env=False) as client:
                await client.get('https://unapproved.invalid/')
        with smoke.no_external_sockets(), guard.installed(), pytest.raises(smoke.base.GuardStop, match='Unbound'):
            asyncio.run(send())
        assert guard.stopped and not guard.calls


def test_experimental_dispatch_scope_rejects_concurrent_unguarded_task(monkeypatch):
    seen = []
    def resolve(host, *args, **kwargs):
        assert host == 'api.siliconflow.cn'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.4', 443))]
    def fake_connect(sock, address):
        seen.append(address)
    guard = smoke.BoundedNetwork()
    async def check():
        monkeypatch.setattr(socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(socket.socket, 'connect', fake_connect)
        ready = asyncio.Event(); done = asyncio.Event()
        async def bounded():
            with guard.dispatch():
                # asyncio resolves in its worker thread without ContextVar propagation.
                await asyncio.get_running_loop().getaddrinfo('api.siliconflow.cn', 443)
                ready.set(); await done.wait()
                with socket.socket() as sock:
                    sock.connect(('192.0.2.4', 443))
                    with pytest.raises(RuntimeError, match='Unapproved'):
                        sock.connect(('192.0.2.5', 443))
                    with pytest.raises(RuntimeError, match='Unapproved'):
                        sock.connect(('192.0.2.4', 80))
        async def outside():
            await ready.wait()
            with socket.socket() as sock, pytest.raises(RuntimeError, match='Unapproved'):
                sock.connect(('192.0.2.4', 443))
            with pytest.raises(RuntimeError, match='DNS'):
                socket.getaddrinfo('unapproved.invalid', 443)
            done.set()
        with guard.installed():
            await asyncio.wait_for(asyncio.gather(bounded(), outside()), 5)
    asyncio.run(check())
    assert seen == [('192.0.2.4', 443)]


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows asyncio connection path')
def test_proof_windows_asyncio_connection_bypasses_socket_hooks(monkeypatch):
    guard = smoke.BoundedNetwork()
    async def check():
        seen = []
        original = socket.socket.connect
        def observe(sock, address):
            seen.append(guard.dispatch_active.get())
            return original(sock, address)
        async def handler(reader, writer):
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        monkeypatch.setattr(socket.socket, 'connect', observe)
        async with server:
            with guard.installed(), guard.dispatch():
                _, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                writer.close()
                await writer.wait_closed()
        # The connection succeeds but the hook is never reached. This is a
        # known safety gap, so the real transport must stay disabled.
        assert seen == []
    asyncio.run(check())


def test_experimental_loop_guard_observes_actual_loopback_connection(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop(); original = loop.create_connection; seen = []
        async def observe(*args, **kwargs):
            seen.append((args[1], args[2])); return await original(*args, **kwargs)
        monkeypatch.setattr(loop, 'create_connection', observe)
        async def handler(reader, writer):
            writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            with smoke.BoundedNetwork().installed():
                _, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                writer.close(); await writer.wait_closed()
        assert len(seen) == 1 and seen[0][0] == '127.0.0.1'
    asyncio.run(check())


def test_experimental_loop_guard_observes_httpx_loopback_connection(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop(); original = loop.create_connection; seen = []
        async def observe(*args, **kwargs):
            seen.append((args[1], args[2])); return await original(*args, **kwargs)
        monkeypatch.setattr(loop, 'create_connection', observe)
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            with smoke.BoundedNetwork().installed():
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/')
        assert response.text == 'OK' and len(seen) == 1
    asyncio.run(check())


def test_experimental_loop_guard_rejects_unguarded_and_unapproved_without_network(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop(); seen = []
        async def fake(protocol_factory, host=None, port=None, *args, **kwargs):
            seen.append((host, port)); return 'synthetic'
        monkeypatch.setattr(loop, 'create_connection', fake)
        guard = smoke.BoundedNetwork(); guard.addresses.add('192.0.2.4')
        with guard.installed():
            with pytest.raises(RuntimeError, match='Unapproved'):
                await loop.create_connection(None, 'api.siliconflow.cn', 443)
            with guard.dispatch():
                with pytest.raises(RuntimeError, match='Unapproved'):
                    await loop.create_connection(None, 'unapproved.invalid', 443)
                with pytest.raises(RuntimeError, match='Unapproved'):
                    await loop.create_connection(None, '192.0.2.5', 443)
                assert await loop.create_connection(None, 'api.siliconflow.cn', 443) == 'synthetic'
                assert await loop.create_connection(None, '192.0.2.4', 443) == 'synthetic'
        assert seen == [('api.siliconflow.cn', 443), ('192.0.2.4', 443)]
    asyncio.run(check())


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows asyncio connection path')
def test_proof_direct_async_sock_connect_bypasses_loop_create_connection(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop(); seen = []
        original = loop.create_connection
        async def observe(*args, **kwargs):
            seen.append(True); return await original(*args, **kwargs)
        monkeypatch.setattr(loop, 'create_connection', observe)
        async def handler(reader, writer):
            writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            with smoke.BoundedNetwork().installed(), socket.socket() as sock:
                sock.setblocking(False)
                await loop.sock_connect(sock, ('127.0.0.1', server.sockets[0].getsockname()[1]))
        assert not seen  # Explicit unguarded path, so real mode stays disabled.
    asyncio.run(check())


def test_persistent_chain_lock_refuses_repeat_before_any_request(tmp_path):
    spec = json.loads(SPEC.read_bytes())
    assert (ROOT / 'var/preferences-acceptance/m2-http-smoke-locks').exists()
    with pytest.raises(FileExistsError):
        smoke.execute(spec, tmp_path / 'never-created')
    assert not (tmp_path / 'never-created').exists()


def test_driver_parses_real_tcp_chunked_sse():
    async def check():
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            payload = b'event: done\ndata: {"ok": true}\n\n'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n' + f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            events = await smoke.read_http(server.sockets[0].getsockname()[1], {})
        assert events == [{'event': 'done', 'data': {'ok': True}}]
    asyncio.run(check())


def test_unknown_attempt_reserves_budget_and_stops_without_retry(tmp_path):
    spec = json.loads(SPEC.read_bytes())
    manifest, _, budget = smoke.preflight(spec)
    guard = smoke.base.BudgetTransport({**manifest, 'budget_cny': budget['remaining_cny']}, tmp_path)
    guard.context = spec['identities'][0]
    sent = []
    async def unknown(*args, **kwargs):
        sent.append(True)
        raise httpx.ReadTimeout('synthetic timeout')
    req = httpx.Request('POST', smoke.base.endpoint(manifest['base_url']),
                        json={'model': manifest['model'], 'messages': []})
    with pytest.raises(smoke.base.GuardStop, match='UNKNOWN'):
        asyncio.run(guard.send(unknown, None, req))
    assert guard.committed == smoke.cont.safety.decimal(guard.calls[0]['reservation_cny'])
    assert smoke.cont.safety.decimal(budget['prior_combined_committed_cny']) + guard.committed <= 1
    with pytest.raises(smoke.base.GuardStop):
        asyncio.run(guard.send(unknown, None, req))
    assert len(sent) == 1


def test_bound_mock_transport_counts_usage_and_caps_output(tmp_path):
    spec = json.loads(SPEC.read_bytes())
    manifest, _, budget = smoke.preflight(spec)
    seen = []
    async def provider(request):
        data = json.loads(request.content); seen.append(data)
        return httpx.Response(200, request=request, json={'id': 'offline-only', 'model': manifest['model'],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})
    guard = smoke.base.BudgetTransport({**manifest, 'budget_cny': budget['remaining_cny']}, tmp_path,
                                       transport=httpx.MockTransport(provider))
    guard.context = spec['identities'][0]
    async def check():
        # This calls the exact accounting dispatch with a synthetic transport;
        # no provider client or real socket is involved.
        async def sender(_client, request, *args, **kwargs):
            return await guard.inner_transport.handle_async_request(request)
        req = httpx.Request('POST', smoke.base.endpoint(manifest['base_url']),
                            json={'model': manifest['model'], 'messages': [], 'max_tokens': 9000})
        return await guard.send(sender, None, req)
    with smoke.no_external_sockets():
        response = asyncio.run(check())
    assert response.status_code == 200 and seen[0]['max_tokens'] == 4096
    assert guard.calls[0]['state'] == 'COMPLETED' and guard.committed == smoke.cont.safety.decimal('0.000044')


def test_both_actual_llm_clients_use_same_budget_guard(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'backend'))
    from app.services.llm import LLMService
    spec = json.loads(SPEC.read_bytes())
    manifest, _, budget = smoke.preflight(spec)
    requests = []
    async def provider(request):
        payload = json.loads(request.content); requests.append(payload)
        return httpx.Response(200, request=request, json={'id': f'synthetic-{len(requests)}',
            'model': manifest['model'], 'object': 'chat.completion', 'created': 1,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'synthetic'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})
    guard = smoke.HistoryCheckedTransport({**manifest, 'budget_cny': budget['remaining_cny']}, tmp_path,
                                          spec, budget, transport=httpx.MockTransport(provider))
    guard.context = spec['identities'][0]
    async def check():
        with smoke.no_external_sockets(), guard.installed():
            async with guard.bind_services():
                service = LLMService(base_url=manifest['base_url'], api_key='synthetic-only', model=manifest['model'])
                first = await service._get_client().chat.completions.create(model=manifest['model'], messages=[{'role': 'user', 'content': 'test'}])
                second = await service.get_chat_model(streaming=False).ainvoke('test')
                assert first.choices[0].message.content == 'synthetic' and second.content == 'synthetic'
    asyncio.run(check())
    assert len(requests) == len(guard.calls) == 2
    assert all(x['state'] == 'COMPLETED' and x['output_token_bound'] == 4096 for x in guard.calls)


def test_checked_transport_blocks_changed_history_before_send(tmp_path, monkeypatch):
    # Preserve real evidence: simulate a changed ledger by replacing the verifier
    # only after preflight. A paid transport must refuse this send.
    spec = json.loads(SPEC.read_bytes())
    manifest, _, budget = smoke.preflight(spec)
    sent = []
    monkeypatch.setattr(smoke.cont, 'verify_history', lambda spec: (_ for _ in ()).throw(ValueError('changed ledger')))
    guard = smoke.HistoryCheckedTransport({**manifest, 'budget_cny': budget['remaining_cny']}, tmp_path, spec, budget)
    guard.context = spec['identities'][0]
    async def synthetic_sender(_client, request, *args, **kwargs):
        sent.append(True)
        return httpx.Response(200, request=request, json={'id': 'offline-proof', 'model': manifest['model'],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})
    request = httpx.Request('POST', smoke.base.endpoint(manifest['base_url']),
                            json={'model': manifest['model'], 'messages': []})
    with pytest.raises(smoke.base.GuardStop, match='Historical evidence'):
        asyncio.run(guard.send(synthetic_sender, None, request))
    assert not sent and guard.stopped


def test_real_guard_blocks_frozen_ledger_hash_change_without_dispatch(tmp_path):
    spec = real_spec(); manifest, _, budget = smoke.preflight(spec, real=True)
    guard = smoke.HistoryCheckedTransport({**manifest, 'budget_cny': budget['remaining_cny']}, tmp_path, spec, budget)
    guard.context = spec['identities'][0]
    spec['history']['prior_runs'][-1]['ledger_sha256'] = '0' * 64
    sent = []
    async def send(*args, **kwargs):
        sent.append(True)
    request = httpx.Request('POST', smoke.base.endpoint(manifest['base_url']),
                            json={'model': manifest['model'], 'messages': []})
    with pytest.raises(smoke.base.GuardStop, match='Historical evidence'):
        asyncio.run(guard.send(send, None, request))
    assert sent == [] and not guard.calls


def test_database_backup_preserves_original(tmp_path):
    spec = json.loads(SPEC.read_bytes())
    source = Path(spec['source_db'])
    before = smoke.base.digest(source.read_bytes())
    smoke.copy_database(source, tmp_path / 'copy.db')
    assert smoke.base.digest(source.read_bytes()) == before == spec['source_db_sha256']
    assert (tmp_path / 'copy.db').stat().st_size < 1024 * 1024


def test_real_tcp_synthetic_unknown_evidence_stops_after_first_request():
    output = ROOT / 'var/preferences-acceptance/m2-http-smoke-synthetic-v4-run'
    started = [json.loads(line) for line in (output / 'started.jsonl').read_bytes().splitlines()]
    trial = json.loads((output / 'trials.jsonl').read_bytes())
    ledger = [json.loads(line) for line in (output / 'provider-ledger.jsonl').read_bytes().splitlines()]
    summary = json.loads((output / 'summary.json').read_bytes())
    assert len(started) == 1 and started[0]['mode'] == 'chat'
    assert trial['state'] == 'FAILED' and trial['guard_stopped'] and trial['provider_states'] == ['UNKNOWN']
    assert trial['error_type'] == 'ValueError' and trial['provider_call_ids'] == [1]
    assert [row['state'] for row in ledger] == ['UNKNOWN_PENDING', 'UNKNOWN']
    assert summary['synthetic_accounting']['committed_including_unknown_cny'] == ledger[-1]['reservation_cny']
    assert summary['historical_real_committed_cny'] == '0.6967552' and summary['real_provider_calls'] == 0
    assert (output / 'http-chat.raw').stat().st_size > 0 and not (output / 'http-agent.raw').exists()
