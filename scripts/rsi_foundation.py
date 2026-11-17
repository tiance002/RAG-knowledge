"""Read-only P-1 readiness gate. No agents, registry writes, replay or promotion.

Mode A remains owned by run_rsi_offline.py. This command validates the proposed
mode B data contract, not an experiment result or permission to optimize.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from run_rsi_offline import checked_case


SCHEMA = 'rsi-foundation-v2.2'
FACT_KINDS = {'field', 'header', 'unit', 'value', 'relation', 'condition', 'scope', 'provenance'}


class FoundationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise FoundationError(code, message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value)
            and set(value) <= set(required) | set(optional), 'CONTRACT_ERROR',
            'Missing or unexpected contract fields')


def parse_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'CONTRACT_ERROR', 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          FoundationError('CONTRACT_ERROR', 'Non-finite JSON number')))


def artifact(root, ref):
    keys(ref, ('path', 'raw_sha256'))
    require(nonempty(ref['path']) and nonempty(ref['raw_sha256']),
            'INPUT_ERROR', 'Artifact path and SHA-256 must be frozen')
    path = (root / ref['path']).resolve()
    require(path.is_relative_to(root.resolve()), 'INPUT_ERROR', 'Artifact escapes its allowed root')
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == ref['raw_sha256'],
            'INPUT_ERROR', 'Artifact bytes differ from frozen SHA-256: ' + ref['path'])
    return path, data


def reviewed(review):
    keys(review, ('kind', 'reviewer', 'review_id', 'approved'))
    require(review['kind'] == 'HUMAN' and review['approved'] is True
            and nonempty(review['reviewer']) and nonempty(review['review_id']),
            'REVIEW_REQUIRED', 'An explicit human review reference is required')


def span(text, start, end, expected):
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text)
            and text[start:end] == expected, 'SOURCE_MISMATCH', 'Source span does not match exact text')


def validate_dataset(spec_path, source_root):
    """Validate all data before R0 registration. Raises with a stable refusal code.

    Source text is a human-reviewed transcription tied to a raw source hash.
    Exact matching checks evidence delivery; it cannot establish semantic truth
    or authenticate a human review. Those remain explicit P-1A responsibilities.
    """
    spec_path, source_root = Path(spec_path).resolve(), Path(source_root).resolve()
    spec_bytes = spec_path.read_bytes()
    spec = parse_json(spec_bytes)
    keys(spec, ('schema_version', 'mode', 'split', 'dataset_version', 'cases', 'evaluation',
                'sources', 'member_spans', 'review'))
    require(spec['schema_version'] == SCHEMA and spec['mode'] == 'B' and spec['split'] == 'DEV',
            'CONTRACT_ERROR', 'Only mode B DEV data preparation is supported')
    require(nonempty(spec['dataset_version']), 'CONTRACT_ERROR', 'Dataset version is required')
    _, case_bytes = artifact(spec_path.parent, spec['cases'])
    _, eval_bytes = artifact(spec_path.parent, spec['evaluation'])
    cases, evaluation = parse_json(case_bytes), parse_json(eval_bytes)
    require(isinstance(cases, list) and len(cases) == 20, 'INSUFFICIENT_REAL_CASES',
            'P-1A requires exactly 20 reviewed real development questions')
    require(isinstance(evaluation, dict), 'CONTRACT_ERROR', 'Evaluation must be keyed by QID')
    reviewed(spec['review'])
    require(isinstance(spec['sources'], list) and bool(spec['sources']),
            'INPUT_ERROR', 'Real source manifest is required')
    sources = {}
    source_hashes = {}
    for source in spec['sources']:
        keys(source, ('source_id', 'file_id', 'kb_id', 'origin', 'raw', 'text', 'review'))
        require(all(nonempty(source[k]) for k in ('source_id', 'file_id', 'kb_id')),
                'CONTRACT_ERROR', 'Nonempty source identity required')
        sid = source['source_id']
        require(sid not in sources and source['origin'] == 'REAL', 'INPUT_ERROR',
                'Source IDs must be unique and origin must be REAL')
        reviewed(source['review'])
        _, raw = artifact(source_root, source['raw'])
        _, text_bytes = artifact(source_root, source['text'])
        require(bool(raw) and bool(text_bytes), 'INPUT_ERROR', 'Empty source is not evidence')
        sources[sid] = {**source, 'body': text_bytes.decode('utf-8-sig')}
        source_hashes[sid] = {'raw': hashlib.sha256(raw).hexdigest(),
                              'text': hashlib.sha256(text_bytes).hexdigest()}
    require(isinstance(spec['member_spans'], dict), 'CONTRACT_ERROR', 'Member spans required')
    ids, questions, rows = set(), set(), []
    for item in cases:
        keys(item, ('qid', 'case'))
        qid, case = item['qid'], item['case']
        require(nonempty(qid) and qid not in ids, 'CONTRACT_ERROR', 'Duplicate or empty QID')
        ids.add(qid)
        require(isinstance(case, dict) and nonempty(case.get('question')), 'CONTRACT_ERROR',
                'A real question is required')
        question = ' '.join(case['question'].split()).casefold()
        require(question not in questions, 'INSUFFICIENT_REAL_CASES', 'Repeated question is not another case')
        questions.add(question)
        # Reuse the executor public projection; reject side-channel annotations.
        require(checked_case(case) == case, 'EVALUATION_LEAK', 'Private fields in executable case')
        require(isinstance(case.get('allowed_kb_ids'), list) and bool(case['allowed_kb_ids']),
                'CONTRACT_ERROR', 'Scoped knowledge base IDs required')
        files = {f['id']: f for f in case['files']}
        chunks = {c['id']: c for c in case['chunks']}
        require(len(files) == len(case['files']) and len(chunks) == len(case['chunks']) and bool(chunks),
                'CONTRACT_ERROR', 'Duplicate or empty file/chunk inventory')
        require(isinstance(case.get('seed_ids'), list) and bool(case['seed_ids'])
                and len(case['seed_ids']) == len(set(case['seed_ids']))
                and set(case['seed_ids']) <= set(chunks)
                and type(case.get('expected_seed_count')) is int
                and case['expected_seed_count'] == len(case['seed_ids']),
                'CONTRACT_ERROR', 'Seeds must uniquely resolve to the frozen chunks')
        require(qid in spec['member_spans'], 'INPUT_ERROR', 'Missing member provenance: ' + qid)
        provenance = spec['member_spans'][qid]
        require(isinstance(provenance, dict) and set(provenance) == set(chunks),
                'INPUT_ERROR', 'Every chunk needs exact source provenance')
        for cid, chunk in chunks.items():
            loc = provenance[cid]
            keys(loc, ('source_id', 'start', 'end', 'locator'))
            require(nonempty(loc['locator']) and loc['source_id'] in sources,
                    'INPUT_ERROR', 'A reviewed original-source locator is required')
            source = sources[loc['source_id']]
            owner = files.get(chunk['file_id'])
            require(owner is not None and owner['kb_id'] in case['allowed_kb_ids']
                    and (source['file_id'], source['kb_id']) == (owner['id'], owner['kb_id']),
                    'SOURCE_MISMATCH', 'Chunk ownership differs from source manifest')
            require(nonempty(chunk['content']) and hashlib.sha256(chunk['content'].encode()).hexdigest()
                    == chunk.get('content_sha256'), 'SOURCE_MISMATCH', 'Chunk hash mismatch')
            span(source['body'], loc['start'], loc['end'], chunk['content'])
        require(qid in evaluation, 'CONTRACT_ERROR', 'Missing evaluation: ' + qid)
        expected = evaluation[qid]
        # Fixed members and desired length directions would predetermine success.
        keys(expected, ('facts', 'review'))
        reviewed(expected['review'])
        require(isinstance(expected['facts'], list) and bool(expected['facts']),
                'CONTRACT_ERROR', 'Each question needs necessary source-bound facts')
        fact_ids = set()
        for fact in expected['facts']:
            keys(fact, ('fact_id', 'kind', 'source_id', 'text', 'start', 'end', 'rule', 'supporting_chunk_ids'))
            require(nonempty(fact['fact_id']) and fact['fact_id'] not in fact_ids
                    and fact['kind'] in FACT_KINDS and nonempty(fact['text'])
                    and fact['rule'] == 'source_bound_exact_span', 'CONTRACT_ERROR', 'Invalid fact contract')
            fact_ids.add(fact['fact_id'])
            require(fact['source_id'] in sources, 'SOURCE_MISMATCH', 'Unknown fact source')
            span(sources[fact['source_id']]['body'], fact['start'], fact['end'], fact['text'])
            support = fact['supporting_chunk_ids']
            require(isinstance(support, list) and bool(support) and len(support) == len(set(support))
                    and set(support) <= set(chunks), 'SOURCE_MISMATCH', 'Fact has no unique member support')
            for cid in support:
                loc = provenance[cid]
                require(loc['source_id'] == fact['source_id'] and loc['start'] <= fact['start']
                        and fact['end'] <= loc['end'], 'SOURCE_MISMATCH',
                        'Fact must occur at the reviewed position in its supporting member')
        rows.append({'qid': qid, 'facts': len(fact_ids), 'chunks': len(chunks), 'status': 'DATA_CHECKED'})
    require(set(evaluation) == ids == set(spec['member_spans']), 'CONTRACT_ERROR', 'Unpaired QIDs')
    return {'schema_version': SCHEMA, 'status': 'DATA_READY', 'dataset_version': spec['dataset_version'],
            'case_count': len(rows), 'cases': rows, 'source_hashes': source_hashes,
            'spec_raw_sha256': hashlib.sha256(spec_bytes).hexdigest(),
            'cases_raw_sha256': hashlib.sha256(case_bytes).hexdigest(),
            'evaluation_raw_sha256': hashlib.sha256(eval_bytes).hexdigest(),
            'execution_status': 'NOT_RUN', 'registry_status': 'NOT_REGISTERED',
            'next_gate': 'P-1C/P-1D: freeze evaluator, register R0 and verify materialize/replay inheritance',
            'limitations': ['Review references are declarations, not authenticated signatures.',
                            'Exact spans verify evidence presence, not semantic answer quality.',
                            'DATA_READY grants no exploration, holdout or promotion eligibility.']}


def audit(spec_path, source_root):
    try:
        return validate_dataset(spec_path, source_root)
    except (FoundationError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {'schema_version': SCHEMA, 'status': 'BLOCKED',
                'error_code': error.code if isinstance(error, FoundationError) else 'INPUT_ERROR',
                'error': str(error), 'execution_status': 'NOT_RUN', 'registry_status': 'NOT_REGISTERED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True,
                        help='Explicit read-only root for raw sources and reviewed text')
    args = parser.parse_args()
    result = audit(args.spec, args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'DATA_READY' else 2


if __name__ == '__main__':
    raise SystemExit(main())
