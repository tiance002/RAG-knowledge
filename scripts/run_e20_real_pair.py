"""Approved four-request E2 replay on ECS; no automatic request resumption."""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def save(path, value):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def continuation_start(bundle, prior):
    """Continue only never-executed cases; an UNKNOWN subject is never replayed."""
    assert prior.get('status') == 'STOPPED'
    old = prior.get('results') or []
    assert len(old) == 1 and old[0]['provider']['status'] == 'UNKNOWN'
    assert (old[0]['qid'], old[0]['arm']) == (bundle['cases'][0]['qid'], bundle['cases'][0]['arm'])
    assert prior.get('model_requests_sent', prior.get('provider_send_attempts')) == 1
    assert prior.get('not_run_cases') == [{'qid':x['qid'],'arm':x['arm']} for x in bundle['cases'][1:]]
    return 1


class AllowedNetwork:
    """Only the approved provider and US telemetry may open outbound sockets."""
    @staticmethod
    def normalize_host(host):
        # AnyIO passes an IDNA bytes hostname to asyncio.getaddrinfo.
        if isinstance(host, bytes):
            host = host.decode('ascii')
        if not isinstance(host, str):
            raise RuntimeError('guard rejected DNS host type')
        return host

    def __enter__(self):
        self.dns = socket.getaddrinfo
        self.connect = socket.socket.connect
        self.connect_ex = socket.socket.connect_ex
        self.ips = set()
        def dns(host, port, *args, **kwargs):
            host = self.normalize_host(host)
            if host not in ('api.siliconflow.cn', 'us.cloud.langfuse.com') or int(port) != 443:
                raise RuntimeError('guard rejected DNS destination before resolution')
            rows = self.dns(host, port, *args, **kwargs)
            self.ips.update(x[4][0] for x in rows)
            return rows
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in self.ips or address[1] != 443:
                raise RuntimeError('unapproved network socket')
            return self.connect(sock, address)
        socket.getaddrinfo = dns
        socket.socket.connect = connect
        socket.socket.connect_ex = lambda sock, address: (connect(sock, address) or 0)
        return self
    def __exit__(self, *args):
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = self.dns, self.connect, self.connect_ex


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approval-id')
    parser.add_argument('--continue-unexecuted', action='store_true')
    parser.add_argument('--prior-summary', type=Path)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding='utf-8'))
    assert bundle['approval_id'] == 'e20-owner-20261030-four-chats' and bundle['limit'] == 4
    assert [(x['qid'], x['arm']) for x in bundle['cases']] == [
        ('office50:031', 'A'), ('office50:031', 'B'), ('office50:014', 'A'), ('office50:014', 'B')]
    for case in bundle['cases']:
        assert sha(canon(case['expected_messages'])) == case['expected_messages_sha256']
        assert sha(canon(case['expanded'])) == case['expanded_snapshot_sha256']
    start_index = 0
    if args.continue_unexecuted:
        assert args.prior_summary is not None
        start_index = continuation_start(bundle, json.loads(args.prior_summary.read_text()))
    out = args.output_dir
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'backend'))
    os.environ.update(DATABASE_URL='sqlite://', RAG_TRACE_SCHEMA='e12',
        RAG_E12_CONTENT_POLICY='LOCAL_ONLY', RAG_OBSERVABILITY_ENABLED='false',
        RAG_PROMETHEUS_ENABLED='false', RAG_GROUNDED_GENERATION='true',
        RAG_PDF_TABLE_GROUNDING='false', RAG_SNAPSHOTS_ENABLED='true',
        RAG_SNAPSHOT_DIR=str(out/'snapshots'), RAG_OBSERVATION_LOG=str(out/'events.jsonl'))
    sys.modules.setdefault('chromadb', MagicMock())
    sys.modules.setdefault('chromadb.config', MagicMock())
    from app.services import observability as obs, chat, spreadsheet_grounding
    from app.services.llm import LLMService
    from app.services.trace_schema import capture_scope
    from app.schemas import Message, MessageRole
    from app.core.config import get_settings
    assert obs.code_fingerprint() == bundle['code_hash']
    assert get_settings().LLM_ANSWER_MAX_TOKENS == bundle['max_tokens'] == 4096
    ids = json.loads(Path('/opt/amrag/ids.json').read_text())['models']
    prod = sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro', uri=True)
    model, endpoint, key = prod.execute('select model_name,base_url,api_key from custom_models where id=?', (ids['vlm'],)).fetchone()
    prod.close()
    assert model == bundle['model'] and sha(endpoint) == bundle['endpoint_sha256'] and key
    source_db = sqlite3.connect('file:' + bundle['index_db_path'] + '?mode=ro', uri=True)
    file_hashes = {}
    for case in bundle['cases']:
        for group in case['expanded']:
            for member in group.get('context_group', {}).get('source_members', []):
                row = source_db.execute('select d.content,f.local_path,f.kb_id from document_chunks d join file_documents f on f.id=d.file_id where d.id=? and f.id=?', (member['id'], member['file_id'])).fetchone()
                assert row and sha(row[0]) == member['content_hash'] and row[2] == case['kb_id']
                if member['file_id'] not in file_hashes:
                    digest = sha(Path(row[1]).read_bytes())
                    assert member['file_id'] == 'office-' + digest
                    file_hashes[member['file_id']] = digest
    source_db.close()
    preflight = {'status': 'PASS', 'model': model, 'endpoint_sha256': sha(endpoint),
        'code_hash': obs.code_fingerprint(), 'bundle_sha256': sha(args.bundle.read_bytes()),
        'source_file_hashes': file_hashes, 'model_requests': 0,
        'context_capacity_evidence': 'SiliconFlow official model page 1024K; immutable input content at most105187UTF8bytes plus framing, no media',
        'actual_input_tokens': 'UNKNOWN', 'sdk_max_retries': 0, 'timeout_seconds': 120}
    preflight.update(planned_new_attempts=4-start_index, preserved_prior_unknown_attempts=start_index,
                     skipped_cases=[{'qid':c['qid'],'arm':c['arm']} for c in bundle['cases'][:start_index]])
    save(out/'preflight.json', preflight)
    if not args.execute:
        print(json.dumps(preflight)); return
    assert args.approval_id == ('e20-owner-20261030-remaining-three' if start_index else bundle['approval_id'])
    assert os.environ.get('LANGFUSE_BASE_URL') == 'https://us.cloud.langfuse.com'
    assert all(os.environ.get(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'))
    os.environ.update(RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true')
    import httpx
    from openai import AsyncOpenAI
    class NullSession:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
    chat.Session = NullSession
    spreadsheet_grounding.prepare_spreadsheet_grounding = lambda *args: None
    sent, input_tokens, output_tokens = start_index, 0, 0
    results = []
    def journal(value):
        with (out/'request-ledger.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + '\n')
            handle.flush(); os.fsync(handle.fileno())
    async def execute():
        nonlocal sent, input_tokens, output_tokens
        for number, case in enumerate(bundle['cases'][start_index:], start=start_index):
            case_out = out/('case-' + str(number))
            case_out.mkdir(mode=0o700)
            run_id = 'e20-real-' + uuid.uuid4().hex
            provider = {}
            class BoundedTransport(httpx.AsyncBaseTransport):
                def __init__(self): self.inner = httpx.AsyncHTTPTransport(retries=0)
                async def handle_async_request(self, request):
                    nonlocal sent
                    if request.method != 'POST' or request.url.host != 'api.siliconflow.cn' or request.url.path != '/v1/chat/completions':
                        raise RuntimeError('unexpected provider call')
                    payload = json.loads(request.content)
                    assert payload['model'] == model and payload['temperature'] == .7 and payload['max_tokens'] == 4096 and payload['stream'] is False
                    assert sha(canon(payload['messages'])) == case['expected_messages_sha256']
                    assert sent == number and sent < 4
                    save(case_out/'actual-provider-request.json', payload)
                    sent += 1
                    provider.update(local_send_id=uuid.uuid4().hex, status='UNKNOWN_PENDING', send_number=sent)
                    journal({'qid': case['qid'], 'arm': case['arm'], 'run_id': run_id, **provider})
                    started = time.monotonic()
                    try:
                        response = await self.inner.handle_async_request(request)
                        data = await response.aread()
                        provider.update(http_status=response.status_code, elapsed_ms=round((time.monotonic()-started)*1000,3), raw_body_sha256=sha(data))
                        body = json.loads(data)
                        visible = {k: body.get(k) for k in ('id', 'model', 'usage')}
                        visible['choices'] = [{'finish_reason': c.get('finish_reason'), 'content': (c.get('message') or {}).get('content')} for c in body.get('choices', [])]
                        # Hidden reasoning is intentionally not persisted.
                        save(case_out/'provider-response-visible.json', visible)
                        provider.update(status='RETURNED' if response.is_success else 'FAILED', provider_request_id=body.get('id'), returned_model=body.get('model'), usage=body.get('usage'))
                        journal({'qid':case['qid'],'arm':case['arm'], 'run_id':run_id, **provider})
                        return response
                    except BaseException as exc:
                        message = str(exc).replace(key, '[REDACTED]')[:1000]
                        status = 'NOT_SENT' if isinstance(exc,RuntimeError) and message.startswith('guard rejected DNS') else 'UNKNOWN'
                        provider.update(status=status, error_type=type(exc).__name__, error_message=message)
                        journal({'qid':case['qid'],'arm':case['arm'],'run_id':run_id,**provider})
                        raise
                async def aclose(self): await self.inner.aclose()
            llm = LLMService(base_url=endpoint, api_key=key, model=model)
            llm._client = AsyncOpenAI(base_url=endpoint, api_key=key, timeout=120, max_retries=0,
                http_client=httpx.AsyncClient(transport=BoundedTransport(), trust_env=False, follow_redirects=False))
            service = chat.ChatService.__new__(chat.ChatService)
            service.llm_service = llm
            async def frozen_retrieval(**kwargs): return copy.deepcopy(case['expanded']), {'retrieval_status':'SUCCESS'}
            service._retrieve_with_new_service = frozen_retrieval
            os.environ['RAG_CHAT_CONTEXT_STRATEGY'] = 'ORIGINAL_CONTENT' if case['arm'] == 'A' else 'EXPANDED_CONTENT'
            dataset = 'e20-frozen-e1-office-replay-v1'
            hashes = sorted(file_hashes.values())
            manifest = {'entries':[{'qid':case['qid'],'dataset_version':dataset,'source_sha256':h,'cloud_allowed':True} for h in hashes]}
            events = []
            error = None
            started = time.monotonic()
            try:
                with capture_scope(run_id=run_id, qid=case['qid'], dataset_version=dataset, source_sha256s=hashes, manifest=manifest, capture_mode='EVAL'):
                    with obs.native_span('qa.request', as_type='chain', run_id=run_id, qid=case['qid'], arm=case['arm'],
                        experiment_id='e20-approved-four-real-v1', dataset_version=dataset,
                        code_hash=obs.code_fingerprint(), index_fingerprint=bundle['index_fingerprint'], mode='chat',
                        retrieval_mode='historical_candidate_snapshot_replay'):
                        obs.observation_io(input=obs.capture(case['question'], content_role='provider_request'))
                        async for event in service._chat_stream_normal([Message(role=MessageRole.USER,content=case['question'])],
                            [case['kb_id']],20,0,None,False,0,None,search_mode='fulltext',context_window=1,use_rewrite=False):
                            events.append(event)
                            journal({'event':'sse_event','qid':case['qid'],'arm':case['arm'],'run_id':run_id,'kind':event[0]})
                        answer = ''.join(d.get('content','') for k,d in events if k=='answer_chunk')
                        obs.observation_io(output=obs.capture(answer,content_role='final_answer'))
                        terminal = events[-1][0] if events else None
                        status = provider.get('status') if terminal == 'done' else 'UNKNOWN' if provider.get('status') == 'UNKNOWN' else 'FAILED'
                        obs.event('e20.real_terminal', execution_status=status, sse_terminal=terminal,
                                  provider_request_id=provider.get('provider_request_id'))
                        active = obs._active_observation.get()
                        if active is not None:
                            active.update(metadata=obs.sanitize({'execution_status':status,'sse_terminal':terminal}))
            except BaseException as exc:
                error = type(exc).__name__
            finally:
                await llm._client.close()
            save(case_out/'sse-events.json', events)
            usage = provider.get('usage') or {}
            prompt_count, completion_count = usage.get('prompt_tokens'), usage.get('completion_tokens')
            counts_known = all(isinstance(x,int) and not isinstance(x,bool) and x>=0 for x in (prompt_count,completion_count))
            if counts_known:
                input_tokens += prompt_count
                output_tokens += completion_count
            row = {'qid':case['qid'],'arm':case['arm'],'run_id':run_id, 'case_directory':str(case_out),
                'actual_model':model,'provider':provider,'exception_type':error,
                'elapsed_ms':round((time.monotonic()-started)*1000,3),
                'answer_sha256':sha(''.join(d.get('content','') for k,d in events if k=='answer_chunk')),
                'terminal':events[-1][0] if events else None,
                'done':next((d for k,d in events if k=='done'),None)}
            save(case_out/'result.json',row); results.append(row)
            print(json.dumps({k:v for k,v in row.items() if k not in ('provider','done')}), flush=True)
            if (provider.get('status') != 'RETURNED' or error or row['terminal'] != 'done'
                    or not counts_known or (row['done'] or {}).get('generation_status') == 'UNKNOWN'
                    or input_tokens > 225000 or output_tokens > 16384):
                break
        obs.flush_observations()
        if obs._client: obs._client.shutdown()
    with AllowedNetwork(): asyncio.run(execute())
    summary = {'status':'COMPLETED' if len(results)==4-start_index and all(r['provider'].get('status')=='RETURNED' and r['terminal']=='done' and (r['done'] or {}).get('generation_status')!='UNKNOWN' for r in results) else 'STOPPED',
        'provider_send_attempts':sent-start_index, 'prior_unknown_attempts':start_index,
        'total_attempts_against_original_four_cap':sent, 'wire_request_count':'UNKNOWN_WITHOUT_RESPONSE',
        'input_tokens_reported':input_tokens,'output_tokens_reported':output_tokens,
        'not_run_cases':[{'qid':c['qid'],'arm':c['arm']} for c in bundle['cases'][start_index+len(results):]],'results':results}
    save(out/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='results'}),flush=True)
    return 0 if summary['status']=='COMPLETED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
