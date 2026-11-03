"""Isolated synthetic ordinary-chat delivery replay. No external network or models."""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
extra = os.getenv('RAG_TEST_EXTRA_SITE_PACKAGES')
if extra:
    sys.path.append(extra)
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
_original_connect, _original_socketpair = socket.socket.connect, socket.socketpair
_inside_socketpair = False

def deny(*args, **kwargs):
    raise RuntimeError('REPLAY_NETWORK_DENIED')

def guarded_connect(sock, address):
    if _inside_socketpair and isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return _original_connect(sock, address)
    return deny()

def guarded_socketpair(*args, **kwargs):
    global _inside_socketpair
    _inside_socketpair = True
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _inside_socketpair = False

socket.socket.connect = guarded_connect
socket.socket.connect_ex = deny
socket.socketpair = guarded_socketpair
socket.create_connection = deny
socket.getaddrinfo = deny
os.environ.update(RAG_GROUNDED_GENERATION='true', RAG_PDF_TABLE_GROUNDING='false',
                  RAG_OBSERVABILITY_ENABLED='false', RAG_SNAPSHOTS_ENABLED='false',
                  RAG_MODEL_ROUTER_MODE='OFF', RAG_CHAT_CONTEXT_STRATEGY='EXPANDED_CONTENT')

from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool
from app.schemas import Message, MessageRole
from app.services import chat, context_builder, spreadsheet_grounding
from replay_source_preparation import prepare_replay_sql, expand_replay_context

class Blocked(Exception):
    pass

def require(value, reason):
    if not value:
        raise Blocked(reason)

def unique_map(rows, label):
    require(isinstance(rows, list), label + ' must be a list')
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'], label + ' invalid ID')
        require(row['id'] not in result, label + ' duplicate ID')
        result[row['id']] = row
    return result

def prepare(case, fixture_path):
    require(isinstance(case, dict) and isinstance(case.get('question'), str) and case['question'], 'missing question')
    scope = case.get('allowed_kb_ids')
    require(isinstance(scope, list) and all(isinstance(x, str) and x for x in scope) and len(set(scope)) == len(scope), 'invalid scope')
    files = unique_map(case.get('files'), 'files')
    chunks = unique_map(case.get('chunks'), 'chunks')
    seeds = case.get('seed_ids')
    count = case.get('expected_seed_count')
    require(isinstance(seeds, list) and all(isinstance(x, str) and x for x in seeds) and len(set(seeds)) == len(seeds), 'invalid seed IDs')
    require(type(count) is int and count >= 0 and count == len(seeds), 'missing or inconsistent expected seed count')
    for file in files.values():
        require(isinstance(file.get('name'), str) and file['name'] and isinstance(file.get('kb_id'), str) and file['kb_id'], 'untrusted file ownership')
    normalized = []
    for c in chunks.values():
        require(c.get('file_id') in files, 'missing FileDocument ownership: ' + str(c.get('file_id')))
        require(isinstance(c.get('content'), str) and type(c.get('original_index')) is int and c['original_index'] >= 0, 'untrusted chunk content or order')
        loc = c.get('source_locator')
        page = loc.get('page') if isinstance(loc, dict) else c.get('page_number')
        normalized.append({**c, 'page_number': page})
    require(all(s in chunks for s in seeds), 'missing seed')
    # Duplicate positional indices make ordered expansion ambiguous.
    positions = [(c['file_id'], c['original_index']) for c in normalized]
    require(len(set(positions)) == len(positions), 'ambiguous chunk order')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    try:
        prepare_replay_sql(engine, chunks=normalized, files=list(files.values()), allowed_kb_ids=scope,
                           ownership_provenance='explicit file ownership in frozen input: ' + str(fixture_path))
        seed_rows = []
        for sid in seeds:
            c = chunks[sid]; f = files[c['file_id']]
            loc = c.get('source_locator') or {}
            seed_rows.append({'id': sid, 'file_id': f['id'], 'file_name': f['name'], 'kb_id': f['kb_id'],
                'content': c['content'], 'page_number': loc.get('page'), 'heading_text': c.get('heading_text'),
                'source_metadata': loc, 'score': 1.0})
        with Session(engine) as session:
            expanded = expand_replay_context(session, seed_rows, allowed_kb_ids=scope, expected_seed_count=count,
                                              clean_zero=(count == 0), window_size=1, max_parent_length=4000)
    finally:
        engine.dispose()
    # Source locators are separate fixture evidence; never infer an absent locator.
    return {'status':'OK', 'question':case['question'], 'allowed_kb_ids':scope, 'seed_ids':seeds,
            'expected_seed_count':count, 'expanded':expanded, 'files':list(files.values()),
            'chunks':list(chunks.values()), 'context_window':1, 'max_parent_length':4000}

def duplicate_key(member, chunk, file):
    loc = chunk.get('source_locator')
    if not isinstance(loc, dict) or not file['name'].lower().endswith('.pdf'):
        return None
    page, bbox = loc.get('page'), loc.get('bbox')
    if not (type(page) is int and page > 0 and isinstance(bbox, list) and len(bbox) == 4
            and all(type(x) in (int, float) for x in bbox) and bbox[0] < bbox[2] and bbox[1] < bbox[3]
            and isinstance(chunk.get('parent_id'), str) and chunk['parent_id']
            and isinstance(chunk.get('heading_text'), str) and chunk['heading_text']):
        return None
    return (member['file_id'], member['kb_id'], member['content'], page, tuple(bbox),
            chunk['parent_id'], chunk['heading_text'], chunk.get('heading_level'))

def select_member_indices(members, chunks, files, seed_ids, selection):
    """Closed, versionable policy seam over verified baseline members.

    Future policies require a reviewed code/config change here; this function
    accepts no question, gold labels, case ID, or executable plugin. It returns
    baseline indices in their original order and must preserve every seed.
    """
    require(selection in ('all', 'exact_duplicate'), 'invalid selection')
    keys = [duplicate_key(m, chunks[m['id']], files[m['file_id']]) for m in members]
    selected = []
    for i, m in enumerate(members):
        remove = (selection == 'exact_duplicate' and m['id'] not in seed_ids and keys[i] is not None
                  and any(keys[j] == keys[i] and (members[j]['id'] in seed_ids or j < i)
                          for j in range(len(members)) if j != i))
        if not remove:
            selected.append(i)
    return selected

class FakeSession:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False

async def deliver(snapshot, config):
    require(snapshot.get('status') == 'OK', 'invalid snapshot')
    require(config.get('selection') in ('all', 'exact_duplicate'), 'invalid selection')
    seeds = snapshot.get('seed_ids'); expanded = snapshot.get('expanded')
    require(isinstance(seeds, list) and isinstance(expanded, list) and len(seeds) == snapshot.get('expected_seed_count'), 'invalid snapshot seeds')
    if not seeds:
        require(not expanded, 'unexpected expansion for empty seed set')
        return {'status':'OK', 'members':[], 'messages':[], 'context':'', 'request_calls':0, 'proof':[]}
    files = unique_map(snapshot.get('files'), 'files'); chunks = unique_map(snapshot.get('chunks'), 'chunks')
    source_members = {}
    for row in expanded:
        require(context_builder._verified_members(row) is not None, 'unverified expansion')
        for m in row['context_group']['source_members']:
            c = chunks.get(m['id']); f = files.get(m.get('file_id'))
            require(c and f and c['file_id'] == f['id'] and f['kb_id'] in snapshot['allowed_kb_ids']
                    and m.get('kb_id') == f['kb_id'] and m['content'] == c['content'], 'untrusted expanded ownership')
            if m['id'] in source_members:
                prior = source_members[m['id']]
                require(all(prior.get(k) == m.get(k) for k in ('id', 'file_id', 'kb_id', 'content', 'page_number', 'parent_id', 'original_index', 'heading_text', 'heading_level')), 'conflicting member identity')
            else:
                source_members[m['id']] = m
    require(all(s in source_members for s in seeds), 'seed omitted from expansion')
    original_build = context_builder.build_chat_context
    captured = {}; calls = []
    def selection(results, formatter, strategy='EXPANDED_CONTENT'):
        citations, summary = original_build(results, formatter, strategy=strategy)
        require(summary['source_mapping_status'] == 'VERIFIED', 'baseline mapping unknown')
        ids = [c['chunk_id'] for c in citations]
        require(len(set(ids)) == len(ids) and all(s in ids for s in seeds), 'baseline seed missing')
        members = []
        for c in citations:
            m = source_members.get(c['chunk_id'])
            require(m and c['content'] == m['content'] and c['fileId'] == m['file_id'] and c['kb_id'] == m['kb_id'], 'baseline member mismatch')
            members.append({**m, 'source_locator':chunks[m['id']].get('source_locator')})
        selected = select_member_indices(members, chunks, files, seeds, config['selection'])
        captured.update(baseline_members=members, members=[members[i] for i in selected],
                        removed_ids=[m['id'] for i,m in enumerate(members) if i not in selected],
                        selected_citations=[citations[i] for i in selected])
        return [citations[i] for i in selected], summary
    async def fake_create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(id='synthetic', model='synthetic', usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content='Synthetic response.'), finish_reason='stop')])
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = SimpleNamespace(model='synthetic', _get_client=lambda:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))))
    async def fake_retrieval(**kwargs): return copy.deepcopy(expanded), {'retrieval_status':'SUCCESS'}
    service._retrieve_with_new_service = fake_retrieval
    original_session = chat.Session
    original_spreadsheet = spreadsheet_grounding.prepare_spreadsheet_grounding
    chat.Session = FakeSession
    spreadsheet_grounding.prepare_spreadsheet_grounding = lambda *args: None
    context_builder.build_chat_context = selection
    try:
        events = [x async for x in service._chat_stream_normal([Message(role=MessageRole.USER, content=snapshot['question'])],
            snapshot['allowed_kb_ids'], max(20,len(seeds)), 0, None, False, 0, None,
            search_mode='fulltext', context_window=1, use_rewrite=False)]
    finally:
        context_builder.build_chat_context = original_build
        chat.Session = original_session
        spreadsheet_grounding.prepare_spreadsheet_grounding = original_spreadsheet
    require(len(calls) == 1 and events and events[-1][0] == 'done', 'ordinary chat did not reach mock provider')
    messages = calls[0]['messages']; prompt = messages[0]['content']
    prefix = '请根据以下参考资料回答用户问题。资料不足时请如实说明，并适当引用来源。\n\n资料：\n'
    require(prompt.startswith(prefix), 'unexpected prompt format')
    context = prompt[len(prefix):]
    citations = captured.pop('selected_citations')
    expected_context = '\n\n'.join(f"[{i}] 来源：{c.get('fileName', '未知')} {c.get('location', '')}\n{c['content']}" for i,c in enumerate(citations, 1))
    require(context == expected_context, 'actual prompt does not match selected numbered citations')
    proof = []
    cursor = 0
    for i, (m, citation) in enumerate(zip(captured['members'], citations), 1):
        header = f"[{i}] 来源：{citation.get('fileName', '未知')} {citation.get('location', '')}\n"
        require(context[cursor:cursor + len(header)] == header, 'numbered prompt header mismatch')
        pos = cursor + len(header)
        needle = m['content']
        require(context[pos:pos + len(needle)] == needle, 'delivered member missing from actual prompt')
        proof.append({'id':m['id'], 'context_start':pos, 'context_end':pos+len(needle), 'prompt_start':len(prefix)+pos, 'prompt_end':len(prefix)+pos+len(needle)})
        cursor = pos + len(needle) + (2 if i < len(citations) else 0)
    return {'status':'OK', **captured, 'messages':messages, 'context':context, 'request_calls':len(calls), 'proof':proof}

def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('prepare'); a.add_argument('case'); a.add_argument('snapshot')
    a = sub.add_parser('deliver'); a.add_argument('snapshot'); a.add_argument('config'); a.add_argument('capture')
    args = p.parse_args()
    output = args.snapshot if args.command == 'prepare' else args.capture
    receipt = {}
    try:
        if args.command == 'prepare':
            result = prepare(json.loads(Path(args.case).read_text(encoding='utf-8')), Path(args.case).resolve())
        else:
            snapshot = json.loads(Path(args.snapshot).read_text(encoding='utf-8'))
            config = json.loads(Path(args.config).read_text(encoding='utf-8'))
            receipt = {'upstream_sha256': hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')).hexdigest(), 'selection': config.get('selection')}
            result = asyncio.run(deliver(snapshot, config))
    except Exception as exc:
        result = {'status':'BLOCKED', 'reason':type(exc).__name__ + ': ' + str(exc)}
    result.update(receipt)
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__': main()
