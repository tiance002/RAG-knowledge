"""Source-relations/v1 tests use invented CONTROL text only."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from rsik.events import canonical, digest
from rsik.exposure import ExposureLedger, validate_snapshot
from rsik.execution_evidence import ExecutionEvidence
from rsik.source_relations import (CATEGORIES, PROFILE, assess, check_fixture,
                                   family_clusters, recompute_coverage, replay_artifacts)
from portable_fixtures import SyntheticCapsule


def _capture(case, *, include=('m1', 'm2'), citations=True):
    chunks = {chunk['id']: chunk for chunk in case['chunks']}
    files = {row['id']: row for row in case['files']}
    members = [{**chunks[mid], 'kb_id': files[chunks[mid]['file_id']]['kb_id']} for mid in include]
    context = '\n'.join(member['content'] for member in members)
    proofs = []
    offset = 0
    for member in members:
        end = offset + len(member['content'])
        proofs.append({'id': member['id'], 'context_start': offset, 'context_end': end,
                       'prompt_start': offset, 'prompt_end': end})
        offset = end + 1
    capture = {'status': 'OK', 'members': members, 'context': context,
               'messages': [{'content': context}], 'proof': proofs,
               'citations': [], 'upstream_sha256': 'synthetic-upstream'}
    return capture


def _review(subject, status='AI_REVIEWED'):
    return {'status': status, 'subject_sha256': digest(subject),
            'reviewer': 'SYNTHETIC_CONTROL_ONLY', 'reviewed_at': '2026-11-15T00:00:00Z'}


def _build_fixture(root, *, partition='CONTROL', source_status='KNOWN_HISTORY', source_review='AI_REVIEWED',
                   atom_review='AI_REVIEWED', exposure_snapshot=None, count=2):
    root = Path(root)
    (root / 'sources').mkdir(parents=True)
    cases, specs = [], {}
    for index in range(count):
        qid = f'CONTROL-{index + 1}'
        source_texts = {'s1': f'Valve A weighs 12 kg. CONTROL case {index}.',
                        's2': f'Valve A is assigned to Depot A only when seal intact. CONTROL case {index}.'}
        sources = {}
        member_spans = {}
        files = []
        chunks = []
        for source_index, sid in enumerate(('s1', 's2')):
            text = source_texts[sid]
            data = text.encode()
            relative_path = f'sources/{qid.lower()}-{sid}.md'
            (root / relative_path).write_bytes(data)
            source_id = f'{qid.lower()}-{sid}'
            file_id = f'{qid.lower()}-file-{sid}'
            kb_id = 'CONTROL-KB'
            family_id = f'CONTROL-FAMILY-{index}-{sid}'
            sources[source_id] = {
                'document_id': f'control-doc-{index}-{sid}', 'version': 'v1',
                'content_sha256': hashlib.sha256(data).hexdigest(),
                'source_family_id': family_id, 'origin': 'INVENTED_TEST_TEXT',
                'relation': 'ORIGINAL', 'related_source_ids': [],
                'exposure_status': source_status, 'review_status': source_review,
                'file_id': file_id, 'kb_id': kb_id, 'relative_path': relative_path,
            }
            files.append({'id': file_id, 'name': f'{sid}.md', 'kb_id': kb_id})
            member_id = f'm{source_index + 1}'
            chunks.append({'id': member_id, 'file_id': file_id, 'content': text,
                           'content_sha256': hashlib.sha256(data).hexdigest()})
            member_spans[member_id] = {'source_id': source_id, 'start': 0, 'end': len(text)}
        case = {'question': 'SYNTHETIC relation question', 'allowed_kb_ids': ['CONTROL-KB'],
                'seed_ids': ['m1'], 'expected_seed_count': 1, 'files': files, 'chunks': chunks}
        supports = [
            {'support_id': 'support-entity-value', 'member_id': 'm1', 'source_id': f'{qid.lower()}-s1',
             'start': 0, 'end': len(source_texts['s1']), 'text': source_texts['s1'],
             'binds': ['entity', 'subject', 'value', 'unit']},
            {'support_id': 'support-condition-negation', 'member_id': 'm2', 'source_id': f'{qid.lower()}-s2',
             'start': 0, 'end': len(source_texts['s2']), 'text': source_texts['s2'],
             'binds': ['object', 'relation', 'scope', 'condition', 'negation']},
        ]
        atom = {'atom_id': f'{qid}-atom', 'components': {
                    'entity': 'Valve A', 'subject': 'Valve A', 'object': 'Depot A',
                    'value': '12', 'unit': 'kg', 'relation': 'assigned to', 'scope': 'Depot A',
                    'condition': 'seal intact', 'negation': 'only when'},
                'categories': sorted(CATEGORIES), 'equivalence_rule': 'EXACT_REVIEWED_SUPPORT_ALTERNATIVES',
                'supports': supports, 'support_alternatives': [['support-entity-value', 'support-condition-negation']],
                'citation_required': True}
        atom['review'] = _review(atom, atom_review)
        specs[qid] = {'profile': PROFILE, 'lineage_id': 'CONTROL-LINEAGE', 'sources': sources,
                      'member_spans': member_spans, 'atoms': [atom]}
        cases.append({'qid': qid, 'case': case})
    (root / 'cases.json').write_bytes(canonical(cases))
    (root / 'relations.restricted.json').write_bytes(canonical(specs))
    manifest = {'profile': PROFILE, 'lineage_id': 'CONTROL-LINEAGE', 'partition': partition,
                'artifact_hashes': {
                    'cases.json': hashlib.sha256((root / 'cases.json').read_bytes()).hexdigest(),
                    'relations.restricted.json': hashlib.sha256((root / 'relations.restricted.json').read_bytes()).hexdigest(),
                }}
    if exposure_snapshot is not None:
        manifest['experiment_id'] = exposure_snapshot['experiment_id']
        manifest['exposure_snapshot'] = exposure_snapshot
    (root / 'manifest.json').write_bytes(canonical(manifest))
    return root


def _append_citations(capture, spec):
    capture['citations'] = []
    for atom in spec['atoms']:
        for support in atom['supports']:
            capture['citations'].append({'atom_id': atom['atom_id'],
                'support_id': support['support_id'], 'source_id': support['source_id'],
                'member_id': support['member_id'], 'start': support['start'], 'end': support['end']})
    return capture


def test_source_relations_requires_complete_exact_support_and_citation(tmp_path):
    fixture = _build_fixture(tmp_path / 'fixture')
    frozen = check_fixture(fixture)
    cases = json.loads((fixture / 'cases.json').read_text(encoding='utf-8'))
    specs = json.loads((fixture / 'relations.restricted.json').read_text(encoding='utf-8'))
    item = cases[0]
    spec = specs[item['qid']]
    source_bytes = {sid: (fixture / row['relative_path']).read_bytes()
                    for sid, row in spec['sources'].items()}
    complete = _append_citations(_capture(item['case']), spec)
    result = assess(item['case'], complete, spec, source_bytes)
    assert frozen['profile'] == PROFILE
    assert result['status'] == 'PRESENT'
    assert {row['status'] for row in result['atoms']} == {'PRESENT'}

    missing_nonseed = _capture(item['case'], include=('m1',))
    assert assess(item['case'], missing_nonseed, spec, source_bytes)['status'] == 'ABSENT'
    copied_missing_support = copy.deepcopy(missing_nonseed)
    copied_missing_support['context'] += '\n' + spec['atoms'][0]['supports'][1]['text']
    copied_missing_support['messages'][0]['content'] = copied_missing_support['context']
    assert assess(item['case'], copied_missing_support, spec, source_bytes)['status'] == 'UNKNOWN'
    missing_citation = _capture(item['case'])
    assert assess(item['case'], missing_citation, spec, source_bytes)['status'] == 'ABSENT'

    forged = copy.deepcopy(complete)
    forged['members'][0]['content'] += ' forged'
    assert assess(item['case'], forged, spec, source_bytes)['status'] == 'UNKNOWN'


def test_coverage_recomputes_six_classes_from_paired_captures(tmp_path):
    fixture = _build_fixture(tmp_path / 'fixture')
    cases = json.loads((fixture / 'cases.json').read_text(encoding='utf-8'))
    specs = json.loads((fixture / 'relations.restricted.json').read_text(encoding='utf-8'))
    controls = []
    for item in cases:
        spec = specs[item['qid']]
        positive = _append_citations(_capture(item['case']), spec)
        negative = _capture(item['case'], include=('m1',))
        positive_path = tmp_path / f'{item["qid"]}-positive.json'
        negative_path = tmp_path / f'{item["qid"]}-negative.json'
        positive_path.write_bytes(canonical(positive)); negative_path.write_bytes(canonical(negative))
        controls.append({'fixture': 'fixture', 'qid': item['qid'], 'atom_id': spec['atoms'][0]['atom_id'],
            'positive_capture': positive_path.name,
            'positive_sha256': hashlib.sha256(positive_path.read_bytes()).hexdigest(),
            'negative_capture': negative_path.name,
            'negative_sha256': hashlib.sha256(negative_path.read_bytes()).hexdigest(),
            'removed_member_ids': ['m2']})
    record = {'schema': 'rsi-source-relations-coverage/v1', 'base_dir': str(tmp_path),
              'review_policy': 'research-source-family/v1',
              'fixtures': ['fixture'], 'controls': controls,
              'status': 'PASS', 'positive_controls_passed': True, 'negative_controls_passed': True}
    result = recompute_coverage(record)
    assert result['status'] == 'PASS'
    assert set(result['matrix']) == CATEGORIES
    assert all(row['status'] == 'PASS' for row in result['matrix'].values())

    changed = copy.deepcopy(record)
    changed['controls'][0]['negative_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='RELATIONS_CONTROL_CAPTURE_CHANGED'):
        recompute_coverage(changed)


def test_source_relations_replay_uses_existing_prepare_deliver_pipeline(tmp_path):
    fixture = _build_fixture(tmp_path / 'fixture')
    cases = json.loads((fixture / 'cases.json').read_text(encoding='utf-8'))
    specs = json.loads((fixture / 'relations.restricted.json').read_text(encoding='utf-8'))

    class CitationControlCapsule(SyntheticCapsule):
        evidence_policy = ExecutionEvidence.synthetic_control()

        def child(self, stage, paths, cwd, **kwargs):
            result = super().child(stage, paths, cwd, **kwargs)
            if stage == 'deliver':
                index = int(Path(cwd).parent.name.removeprefix('case-')) - 1
                item = cases[index]
                spec = specs[item['qid']]
                result['citations'] = [
                    {'atom_id': atom['atom_id'], 'support_id': support['support_id'],
                     'source_id': support['source_id'], 'member_id': support['member_id'],
                     'start': support['start'], 'end': support['end']}
                    for atom in spec['atoms'] for support in atom['supports']]
                Path(paths[-1]).write_bytes(canonical(result))
            return result

    worker = (Path(__file__).resolve().parents[2] / 'scripts/rsi_delivery_worker.py').read_bytes()
    artifacts = {arm: {'candidate_id': f'CONTROL-{arm}', 'worker': worker,
                       'config': canonical({'selection': 'all'})}
                 for arm in ('A', 'B')}
    result = replay_artifacts(CitationControlCapsule(), fixture, artifacts, tmp_path / 'run',
        deadline=10**12, schema=PROFILE, scope='SYNTHETIC_CONTROL_PIPELINE_NOT_REAL_EXECUTION')
    assert result['evaluator_profile'] == PROFILE
    assert result['evidence_mode'] == 'SYNTHETIC_CONTROL'
    assert result['real_eligible'] is False
    assert len(result['per_question']) == 2
    assert all(row['parent_facts'] == row['candidate_facts'] for row in result['per_question'])


def _ledger_source(source_id, family_id, content, *, relation='ORIGINAL', parents=(), exposure='KNOWN_HISTORY'):
    source = {'source_id': source_id, 'document_id': source_id, 'version': 'v1',
        'content_sha256': hashlib.sha256(content).hexdigest(), 'source_family_id': family_id,
        'origin': 'SYNTHETIC_CONTROL', 'relation': relation, 'related_source_ids': list(parents),
        'exposure_status': exposure}
    review = {'status': 'HUMAN_REVIEWED', 'subject_sha256': digest(source),
              'reviewer': 'SYNTHETIC_CONTROL_ONLY', 'reviewed_at': '2026-11-15T00:00:00Z'}
    return source, review


def test_exposure_ledger_clusters_shared_families_and_invalidates_snapshots(tmp_path):
    ledger = ExposureLedger(tmp_path / 'events.sqlite')
    source, review = _ledger_source('s1', 'family-a', b'same bytes')
    ledger.register(source, review=review)
    reprint, review = _ledger_source('s2', 'family-a', b'same bytes', relation='REPRINT_OF', parents=('s1',))
    ledger.register(reprint, review=review)
    fresh, review = _ledger_source('s3', 'family-b', b'fresh bytes')
    ledger.register(fresh, review=review)

    clusters = ledger.dependency_clusters([
        {'qid': 'q1', 'source_ids': ['s1']}, {'qid': 'q2', 'source_ids': ['s2']},
        {'qid': 'q3', 'source_ids': ['s3']}])
    assert clusters['clusters'] == [['q1', 'q2'], ['q3']]
    assert clusters['statistical_denominator'] == 2

    snapshot = ledger.snapshot(['s3'], experiment_id='CONTROL-EXPERIMENT')
    assert validate_snapshot(snapshot, require_final=True)['eligible'] is True
    ledger.expose(['s3'], partition='DEV', experiment_id='prior-dev', actor='CONTROL',
                  evidence_ref='control-record', occurred_at='2026-11-15T00:00:00Z', event_key='dev-exposure')
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_SNAPSHOT_CHANGED'):
        validate_snapshot(snapshot, require_final=True)


def test_unknown_and_seen_sources_are_blocked_from_final_holdout(tmp_path):
    ledger = ExposureLedger(tmp_path / 'events.sqlite')
    unknown, review = _ledger_source('unknown', 'provisional-hash-a', b'unknown',
        relation='UNKNOWN', exposure='EXPOSURE_UNKNOWN')
    review = {'status': 'UNREVIEWED', 'subject_sha256': digest(unknown),
              'reviewer': 'SYNTHETIC_METADATA_IMPORT', 'reviewed_at': '2026-11-15T00:00:00Z'}
    ledger.register(unknown, review=review)
    unknown_other, review = _ledger_source('unknown-other', 'provisional-hash-b', b'unknown other',
        relation='UNKNOWN', exposure='EXPOSURE_UNKNOWN')
    review = {'status': 'UNREVIEWED', 'subject_sha256': digest(unknown_other),
              'reviewer': 'SYNTHETIC_METADATA_IMPORT', 'reviewed_at': '2026-11-15T00:00:00Z'}
    ledger.register(unknown_other, review=review)
    ledger.expose(['unknown'], partition='DEV', experiment_id='CONTROL-OLD-USE', actor='CONTROL',
        evidence_ref='control-record', occurred_at='2026-11-15T00:00:00Z', event_key='unknown-family-use')
    snapshot = ledger.snapshot(['unknown-other'], experiment_id='CONTROL-FINAL')
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_FINAL_INELIGIBLE'):
        validate_snapshot(snapshot, require_final=True)
    assert snapshot['dependency_cluster_ids'] == ['__UNKNOWN_SOURCE_FAMILY__', 'provisional-hash-b']
    assert 'SOURCE_FAMILY_ALREADY_EXPOSED' in snapshot['blocking_reasons']

    cases = [{'qid': 'q1'}, {'qid': 'q2'}]
    specs = {'q1': {'sources': {'s1': {'relation': 'UNKNOWN', 'source_family_id': 'provisional-a'}}},
             'q2': {'sources': {'s2': {'relation': 'UNKNOWN', 'source_family_id': 'provisional-b'}}}}
    grouped = family_clusters(cases, specs)
    assert grouped['q1'] == grouped['q2']

    known_history, review = _ledger_source('bad-unknown', 'provisional-hash-b', b'bad',
        relation='UNKNOWN', exposure='KNOWN_HISTORY')
    with pytest.raises(ValueError, match='UNKNOWN_RELATION_REQUIRES_UNKNOWN_EXPOSURE'):
        ledger.register(known_history, review=review)


def test_research_policy_accepts_fresh_ai_review_but_not_production(tmp_path):
    ledger = ExposureLedger(tmp_path / 'events.sqlite')
    fixture = _build_fixture(tmp_path / 'fixture', source_review='AI_REVIEWED', atom_review='AI_REVIEWED')
    specs = json.loads((fixture / 'relations.restricted.json').read_text(encoding='utf-8'))
    source_ids = sorted({sid for spec in specs.values() for sid in spec['sources']})
    for spec in specs.values():
        for source_id, source in spec['sources'].items():
            ledger_source = {'source_id': source_id, 'document_id': source['document_id'],
                'version': source['version'], 'content_sha256': source['content_sha256'],
                'source_family_id': source['source_family_id'], 'origin': source['origin'],
                'relation': source['relation'], 'related_source_ids': [],
                'exposure_status': 'KNOWN_HISTORY'}
            review = {'status': 'AI_REVIEWED', 'subject_sha256': digest(ledger_source),
                'reviewer': 'SYNTHETIC_CONTROL_AI_REVIEW', 'reviewed_at': '2026-11-15T00:00:00Z'}
            ledger.register(ledger_source, review=review)

    snapshot = ledger.snapshot(source_ids, experiment_id='CONTROL-RESEARCH',
        review_policy='research-source-family/v1')
    assert snapshot['eligible'] is True
    assert snapshot['production_review_eligible'] is False
    assert snapshot['production_qualified'] is False
    assert snapshot['high_risk_review_pending'] is True
    assert validate_snapshot(snapshot, require_final=True,
        review_policy='research-source-family/v1')['eligible'] is True
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_SNAPSHOT_INVALID'):
        validate_snapshot(snapshot, require_final=True)

    manifest = json.loads((fixture / 'manifest.json').read_text(encoding='utf-8'))
    manifest.update(partition='FINAL_HOLDOUT', experiment_id='CONTROL-RESEARCH',
                    exposure_snapshot=snapshot)
    (fixture / 'manifest.json').write_bytes(canonical(manifest))
    research_freeze = check_fixture(fixture, review_policy='research-source-family/v1')
    assert research_freeze['production_review_eligible'] is False
    assert research_freeze['production_qualified'] is False
    assert research_freeze['high_risk_review_pending'] is True
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_SNAPSHOT_INVALID'):
        check_fixture(fixture)

    ledger.expose([source_ids[0]], partition='DEV', experiment_id='prior-dev', actor='CONTROL',
        evidence_ref='control-record', occurred_at='2026-11-15T00:00:00Z', event_key='research-seen')
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_SNAPSHOT_CHANGED'):
        validate_snapshot(snapshot, require_final=True, review_policy='research-source-family/v1')


def test_final_fixture_requires_unchanged_fresh_family_snapshot(tmp_path):
    ledger = ExposureLedger(tmp_path / 'events.sqlite')
    fixture_root = tmp_path / 'fixture'
    placeholder = _build_fixture(fixture_root, partition='CONTROL')
    specs = json.loads((placeholder / 'relations.restricted.json').read_text(encoding='utf-8'))
    source_ids = sorted({sid for spec in specs.values() for sid in spec['sources']})
    for spec in specs.values():
        for source_id, source in spec['sources'].items():
            ledger_source = {'source_id': source_id, 'document_id': source['document_id'],
                'version': source['version'], 'content_sha256': source['content_sha256'],
                'source_family_id': source['source_family_id'], 'origin': source['origin'],
                'relation': source['relation'], 'related_source_ids': [],
                'exposure_status': 'KNOWN_HISTORY'}
            review = {'status': 'HUMAN_REVIEWED', 'subject_sha256': digest(ledger_source),
                'reviewer': 'SYNTHETIC_CONTROL_ONLY', 'reviewed_at': '2026-11-15T00:00:00Z'}
            ledger.register(ledger_source, review=review)
            source['exposure_status'] = 'KNOWN_HISTORY'
            source['review_status'] = 'HUMAN_REVIEWED'
        for atom in spec['atoms']:
            atom['review'] = _review({k: v for k, v in atom.items() if k != 'review'}, 'HUMAN_REVIEWED')
    (fixture_root / 'relations.restricted.json').write_bytes(canonical(specs))
    cases = json.loads((fixture_root / 'cases.json').read_text(encoding='utf-8'))
    snapshot = ledger.snapshot(source_ids, experiment_id='CONTROL-FINAL')
    manifest = {'profile': PROFILE, 'lineage_id': 'CONTROL-LINEAGE', 'partition': 'FINAL_HOLDOUT',
        'experiment_id': 'CONTROL-FINAL', 'exposure_snapshot': snapshot,
        'artifact_hashes': {'cases.json': hashlib.sha256((fixture_root / 'cases.json').read_bytes()).hexdigest(),
            'relations.restricted.json': hashlib.sha256((fixture_root / 'relations.restricted.json').read_bytes()).hexdigest()}}
    (fixture_root / 'manifest.json').write_bytes(canonical(manifest))
    assert check_fixture(fixture_root)['partition'] == 'FINAL_HOLDOUT'

    ledger.expose([source_ids[0]], partition='DEV', experiment_id='prior', actor='CONTROL',
        evidence_ref='control-record', occurred_at='2026-11-15T00:00:00Z', event_key='prior-use')
    with pytest.raises(ValueError, match='SOURCE_EXPOSURE_SNAPSHOT_CHANGED'):
        check_fixture(fixture_root)
