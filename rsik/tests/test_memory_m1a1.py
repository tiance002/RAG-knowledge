"""Versioned memory receipt controls; local synthetic metadata, no paid calls."""
from rsik.analytics.metrics import compute_metrics
from rsik.analytics.projections import project_sources
from rsik.handbook.store import Handbook, RetrievalReceipt
from rsik.observability import Observer
from rsik.tests.test_observation_analytics import source


VERSION = 'rsi-metrics/m1a1'


def metrics(events):
    events = [(kind, dict(metric_contract_version=VERSION, **payload)) for kind, payload in events]
    result = compute_metrics(project_sources([source(*events)]))
    assert len(result['groups']) == 1
    group = result['groups'][0]
    assert group['metric_contract_version'] == VERSION
    return {m['metric_id']: m for m in group['metrics']}


def test_two_operations_and_only_first_injected():
    events = [
        ('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r1', entry_ids=['E1', 'E2'])),
        ('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r2', entry_ids=['E1', 'E3'])),
        ('MEMORY_INJECTED', dict(retrieval_id='r1', entry_ids=['E1', 'E2'])),
        ('PROPOSAL_REFERENCED', dict(entry_ids=['E1'], proposal_instance_id='p')),
    ]
    result = metrics(events)
    assert result['M1']['value'] == 2
    assert (result['M2']['numerator'], result['M2']['denominator']) == (2, 4)
    assert (result['M3']['numerator'], result['M3']['denominator']) == (1, 2)
    # Different event IDs repeating the same facts cannot increase a denominator.
    duplicate = metrics(events + events)
    assert [(duplicate[m]['numerator'], duplicate[m]['denominator']) for m in ('M1', 'M2', 'M3')] == [
        (2, None), (2, 4), (1, 2)]


def test_empty_retrieval_is_an_operation_and_empty_entry_denominator():
    result = metrics([('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='empty', entry_ids=[], returned_count=0))])
    assert result['M1']['value'] == 1
    assert (result['M2']['numerator'], result['M2']['denominator'], result['M2']['status']) == (0, 0, 'N/A')
    assert result['M3']['status'] == 'UNKNOWN'


def test_round_reference_pairs_deduplicate_within_round_only():
    events = []
    for round_id in ('round-a', 'round-b'):
        events.extend([
            ('MEMORY_RETRIEVAL_COMPLETED', dict(round_id=round_id, retrieval_id=round_id, entry_ids=['E1'])),
            ('MEMORY_INJECTED', dict(round_id=round_id, retrieval_id=round_id, entry_ids=['E1'])),
        ])
    events += [('PROPOSAL_REFERENCED', dict(round_id='round-a', entry_ids=['E1'])),
               ('PROPOSAL_REFERENCED', dict(round_id='round-a', entry_ids=['E1']))]
    result = metrics(events)
    assert (result['M3']['numerator'], result['M3']['denominator']) == (1, 2)


def test_no_attribution_from_match_or_missing_operation_identity():
    result = metrics([
        ('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r', entry_ids=['E1'])),
        ('EXPERIENCE_MATCHED', dict(entry_id='E1')),
        ('MEMORY_INJECTED', dict(entry_ids=['E1'])),
        ('PROPOSAL_REFERENCED', dict(entry_ids=['E1'])),
    ])
    assert result['M2']['status'] == 'UNKNOWN'
    assert result['M3']['value'] == 1  # Explicit injection and round identity exist.
    result = metrics([('EXPERIENCE_MATCHED', dict(entry_id='E1')),
                      ('PROPOSAL_REFERENCED', dict(entry_ids=['E1']))])
    assert result['M3']['status'] == 'UNKNOWN'


def test_legacy_and_new_contracts_never_merge():
    events = [('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r', entry_ids=[], returned_count=0)),
              ('MEMORY_RETRIEVAL_COMPLETED', dict(metric_contract_version=VERSION, retrieval_id='r', entry_ids=[], returned_count=0))]
    groups = compute_metrics(project_sources([source(*events)]))['groups']
    assert len(groups) == 2
    old = next(g for g in groups if 'metric_contract_version' not in g)
    new = next(g for g in groups if g.get('metric_contract_version') == VERSION)
    assert next(m for m in old['metrics'] if m['metric_id'] == 'M1')['value'] == 0
    assert next(m for m in new['metrics'] if m['metric_id'] == 'M1')['value'] == 1


def test_runtime_empty_receipts_have_distinct_operation_ids(tmp_path):
    store = Handbook(tmp_path / 'handbook', evidence_mode='SYNTHETIC_CONTROL')
    observer = Observer(tmp_path / 'observations.sqlite', {'round_id': 'round-a', 'evidence_mode': 'SYNTHETIC_CONTROL'})
    receipts = [store.retrieve('dedup', 'OTHER', {}, 'fp', observer=observer) for _ in range(2)]
    assert all(isinstance(r, RetrievalReceipt) and r == [] for r in receipts)
    assert receipts[0].retrieval_id != receipts[1].retrieval_id
    events = observer.log.read()
    assert all(e['payload']['metric_contract_version'] == VERSION for e in events)
    rows = project_sources([dict(source_id='runtime', sha256='a'*64, path='synthetic/runtime', events=events)])
    result = compute_metrics(rows)['groups'][0]['metrics']
    assert next(m for m in result if m['metric_id'] == 'M1')['value'] == 2


def test_missing_round_identity_is_unknown_not_inferred_from_proposal():
    result = metrics([
        ('PROPOSAL_GENERATED', dict(proposal_instance_id='p', round_id='known-round')),
        ('MEMORY_INJECTED', dict(round_id=None, entry_ids=['E1'])),
        ('PROPOSAL_REFERENCED', dict(round_id=None, proposal_instance_id='p', entry_ids=['E1'])),
    ])
    assert result['M3']['status'] == 'UNKNOWN'


def test_query_workflow_injection_keeps_origin_receipts(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path
    from rsik import query_workflow, query_learning
    from rsik.tests.test_handbook_and_promotion import entry, evidence_context, delta
    helpers = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests' / 'test_query_workflow.py'))
    kwargs, _, _ = helpers['setup'](monkeypatch, tmp_path)
    monkeypatch.setattr(query_learning, 'record_query_round_learning', lambda **kw: None)
    store = Handbook(tmp_path / 'handbook', evidence_mode='SYNTHETIC_CONTROL')
    for section in ('dedup', 'budget', 'order', 'format'):
        body = entry()
        body['section'] = section
        store.commit_delta(delta(body), evidence_context(True), idempotency_key=section)
    kwargs.update(handbook=store, compatibility_fingerprint='SYNTHETIC-compatible', features={'same_source': True})
    query_workflow.run_query_round(**kwargs)
    observer = Observer(kwargs['directory'] / 'observations.sqlite')
    completed = [e['payload'] for e in observer.log.read() if e['event_type'] == 'MEMORY_RETRIEVAL_COMPLETED']
    injected = [e['payload'] for e in observer.log.read() if e['event_type'] == 'MEMORY_INJECTED']
    assert len(completed) == 4
    assert sum(len(p['entry_ids']) for p in injected) == 3
    by_id = {p['retrieval_id']: set(p['entry_ids']) for p in completed}
    assert all(set(p['entry_ids']) <= by_id[p['retrieval_id']] for p in injected)
    assert 'retrieval_id' not in query_workflow.load(kwargs['directory'] / 'public-context.json')['handbook_entries'][0]


def test_started_interrupted_retrieval_counts_without_inventing_empty_result():
    result = metrics([('MEMORY_RETRIEVAL_STARTED', dict(retrieval_id='interrupted'))])
    assert result['M1']['value'] == 1
    assert result['M2']['status'] == 'UNKNOWN'
    result = metrics([
        ('MEMORY_RETRIEVAL_STARTED', dict(retrieval_id='done')),
        ('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='done', entry_ids=[], returned_count=0)),
        ('MEMORY_RETRIEVAL_STARTED', dict(retrieval_id='interrupted')),
    ])
    assert result['M1']['value'] == 2
    assert result['M2']['status'] == 'UNKNOWN'


def test_new_report_declares_contract_in_json_and_markdown(tmp_path):
    from rsik.analytics.report import build_report, render_markdown
    observer = Observer(tmp_path / 'observations.sqlite', {'round_id': 'round-a', 'evidence_mode': 'SYNTHETIC_CONTROL'})
    observer.emit('MEMORY_RETRIEVAL_STARTED', {'retrieval_id': 'empty'})
    observer.emit('MEMORY_RETRIEVAL_COMPLETED', {'retrieval_id': 'empty', 'entry_ids': [], 'returned_count': 0})
    report = build_report([observer.path])
    assert report['metrics_schema'] == VERSION
    assert report['metric_contract_versions'] == [VERSION]
    markdown = render_markdown(report)
    assert VERSION in markdown and 'retrieval_id + entry_id' in markdown
    assert 'M0 完成后停止；未实施 M1。' not in markdown
