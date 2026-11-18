"""DEV-only S3.4 inventory. Runs the frozen FileParser twice, without retrieval."""
from collections import Counter
import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def locate(text, span):
    """Locate exact or whitespace-normalized containment; never assign semantics."""
    if span in text:
        return 'EXACT'
    if ' '.join(span.split()) in ' '.join(text.split()):
        return 'WHITESPACE_NORMALIZED'
    return None


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('manifest', type=Path, help='Local DEV-only source paths and hashes')
    cli.add_argument('output', type=Path, help='Fresh isolated output directory')
    args = cli.parse_args()
    sources = json.loads(args.manifest.read_bytes())
    if not sources or any(s['partition'] != 'DEV_ONLY' for s in sources):
        cli.error('Only explicit DEV-only inputs are accepted')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    data = {}
    for s in sources:
        path = Path(s['path']).resolve(strict=True)
        if path.suffix not in {'.txt', '.md', '.csv', '.pdf'} or digest(path.read_bytes()) != s['sha256']:
            raise ValueError('SOURCE_NOT_BOUND')
        if s['source_id'] in data:
            raise ValueError('DUPLICATE_SOURCE')
        data[s['source_id']] = path

    def is_within(path):
        return Path(os.fsdecode(path)).resolve().is_relative_to(output)

    def audit(event, arguments):
        if event in ('socket.gethostname',):
            return
        if event.startswith(('socket.', 'subprocess.', 'sqlite3.connect')) or event == 'os.system':
            raise RuntimeError('S34_ISOLATION_DENIED_' + event)
        if event == 'open':
            path, _, flags = arguments
            if not isinstance(path, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                if not is_within(path):
                    raise RuntimeError('S34_ESCAPE_WRITE')
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir'} and not is_within(arguments[0]):
            raise RuntimeError('S34_ESCAPE_WRITE')
        elif event == 'os.rename' and not all(is_within(path) for path in arguments[:2]):
            raise RuntimeError('S34_ESCAPE_WRITE')

    os.chdir(output)
    sys.dont_write_bytecode = True
    os.environ.update(IMAGE_DIR=str(output/'images'), PDF_AUDIT_DIR=str(output/'manifests'),
                      CHROMA_DB_DIR=str(output/'unused-index'), UPLOAD_DIR=str(output/'unused-uploads'),
                      DATABASE_URL='sqlite:///:memory:', RAG_OBSERVABILITY_ENABLED='false',
                      RAG_PROMETHEUS_ENABLED='false', PDF_PARSER_MODE='legacy')
    sys.path.insert(0, str(repo/'backend'))
    sys.addaudithook(audit)

    from app.services.parser import FileParser
    config = dict(chunk_mode='custom', chunk_size=500, chunk_overlap=50, separator='\n\n',
                  heading_level=2, use_semantic=False, embedding_max_tokens=8192)
    packages = {}
    for name in ('pymupdf', 'pandas', 'python-docx', 'tiktoken', 'langchain-text-splitters',
                 'langchain-experimental', 'langchain-community', 'pydantic-settings'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'NOT_INSTALLED'
    fingerprints = {file: digest((repo/file).read_bytes()) for file in (
        'backend/app/services/parser.py', 'backend/app/services/pdf_provenance.py',
        'backend/app/core/config.py', 'scripts/audit_s34_native.py')}
    receipt = {'schema': 'rsi-s34-native-members/v1', 'scope': 'ISOLATED_PARSER_OUTPUT',
               'production_index_identity': 'UNKNOWN_NOT_QUERIED',
               'source_count': len(sources), 'config': config, 'inputs': fingerprints,
               'environment': {'python': platform.python_version(), 'system': platform.system(),
                               'packages': packages}, 'seed_selection': 'UNKNOWN_NOT_RUN',
               'network_calls': 0, 'model_calls': 0, 'formal_index_writes': 0, 'sources': []}
    (output/'members').mkdir()
    for s in sources:
        source_id = s['source_id']
        raw_path = data[source_id]
        if not source_id or not source_id.replace('-', '').isalnum():
            raise ValueError('INVALID_SOURCE_ID')
        first, second = FileParser('s34'), FileParser('s34')
        a, b = [(c.content, c.page_number, c.content_type, c.token_count)
                for c in first.parse(str(raw_path), **config)], [
                    (c.content, c.page_number, c.content_type, c.token_count)
                    for c in second.parse(str(raw_path), **config)]
        if not a or a != b or any(not c[0].strip() for c in a):
            raise ValueError('NATIVE_PARSE_NOT_REPRODUCIBLE')
        if digest(raw_path.read_bytes()) != s['sha256']:
            raise ValueError('SOURCE_CHANGED_DURING_PARSE')
        member_ids = []
        members = []
        splitter = 'CL100K_BASE' if first.splitter.langchain_splitter is not None else 'NATIVE_PARSER_CHARACTER_FALLBACK'
        for index, (text, page, kind, tokens) in enumerate(a):
            content_sha = digest(text.encode())
            identity = digest(canonical([s['sha256'], fingerprints, config, packages, splitter, index, content_sha]))
            member_id = 's34-native-' + identity[:24]
            member_ids.append(member_id)
            members.append({'member_id': member_id, 'ordinal': index, 'content_sha256': content_sha,
                            'characters': len(text), 'page_number': page, 'content_type': kind,
                            'token_count': tokens, 'seed': 'UNKNOWN_NOT_RUN'})
        (output/'members'/(source_id+'.json')).write_bytes(canonical([
            {**member, 'content': record[0]} for member, record in zip(members, a, strict=True)]))
        spans = []
        if s.get('spans'):
            with raw_path.open(encoding='utf-8', newline='') as f:
                source_text = f.read()
            for span in s['spans']:
                excerpt = source_text[span['start']:span['end']]
                if digest(excerpt.encode()) != span['sha256']:
                    raise ValueError('SPAN_SOURCE_CHANGED')
                hits = [{'member_id': mid, 'match': match} for mid, record in zip(member_ids, a, strict=True)
                        if (match := locate(record[0], excerpt))]
                spans.append({**span, 'matches': hits, 'support_review': 'UNKNOWN_HUMAN_REVIEW_REQUIRED'})
        receipt['sources'].append({'source_id': source_id, 'source_sha256': s['sha256'], 'effective_splitter': splitter,
                                   'members': members, 'span_checks': spans, 'parse_repeat_identical': True,
                                   'chunk_types': dict(Counter(m['content_type'] for m in members))})
        print(source_id, 'members', len(members), 'splitter', splitter, 'locator spans', len(spans), flush=True)
    (output/'native-map.json').write_bytes(canonical(receipt) + b'\n')


if __name__ == '__main__':
    main()
